"""
聊天路由
"""
import asyncio
import hashlib
import json
from datetime import datetime
from types import SimpleNamespace
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc
from sqlalchemy.orm import selectinload
from loguru import logger

from app.config import settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.conversation import Conversation, Message, MessageRole, MessageType
from app.models.knowledge import KnowledgeBase
from app.schemas.chat import (
    ConversationCreate, ConversationResponse, ConversationListResponse,
    MessageResponse, ChatRequest, SaveStoppedMessageRequest, ConversationCompactResponse,
    ChatContextPreviewRequest, ChatContextPreviewResponse,
)
from app.services.llm_service import LLMService
from app.services.agent_tools import get_tool_registry
from app.services.agent_runtime_service import get_agent_runtime_service
from app.services.chat_context_store import ConversationItemStreamStore
from app.services.conversation_context_compaction_service import (
    ConversationItemStreamUnavailableError,
    get_conversation_context_compaction_service,
)

router = APIRouter()


def _normalized_optional_text(value: object) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.lower() in {"none", "null"}:
        return None
    return text


def _normalized_versioned_payload(payload: object) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    version = str(payload.get("version") or "").strip()
    if not version:
        return None
    normalized = dict(payload)
    normalized["version"] = version
    return normalized


def _normalized_context_snapshot_payload(payload: object) -> Optional[dict]:
    normalized = _normalized_versioned_payload(payload)
    if normalized is None:
        return None
    normalized["context_state"] = _normalized_context_state_payload(normalized.get("context_state"))
    normalized["compacted_history"] = _normalized_compacted_history_payload(normalized.get("compacted_history"))
    return normalized


def _normalized_evidence_entry(payload: object) -> Optional[dict]:
    if isinstance(payload, str):
        summary = str(payload).strip()
        if not summary:
            return None
        return {
            "entry_id": f"evidence:{hashlib.sha256(summary.lower().encode('utf-8')).hexdigest()[:16]}",
            "origin_kind": "llm_inferred",
            "summary": summary,
            "status": "confirmed",
            "source_labels": [],
            "tool_names": [],
            "turn_ids": [],
            "tool_call_ids": [],
        }
    if not isinstance(payload, dict):
        return None
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        return None
    entry_id = str(payload.get("entry_id") or "").strip() or f"evidence:{hashlib.sha256(summary.lower().encode('utf-8')).hexdigest()[:16]}"
    origin_kind = str(payload.get("origin_kind") or "llm_inferred").strip().lower()
    if origin_kind not in {"tool_result", "assistant_summary", "llm_inferred"}:
        origin_kind = "llm_inferred"
    status = str(payload.get("status") or "confirmed").strip().lower()
    if status not in {"confirmed", "provisional"}:
        status = "confirmed"
    source_labels = [
        str(item).strip()
        for item in list(payload.get("source_labels") or [])
        if str(item).strip()
    ][:6]
    tool_names = [
        str(item).strip()
        for item in list(payload.get("tool_names") or [])
        if str(item).strip()
    ][:4]
    turn_ids = [
        str(item).strip()
        for item in list(payload.get("turn_ids") or [])
        if str(item).strip()
    ][:6]
    tool_call_ids = [
        str(item).strip()
        for item in list(payload.get("tool_call_ids") or [])
        if str(item).strip()
    ][:8]
    return {
        "entry_id": entry_id,
        "origin_kind": origin_kind,
        "summary": summary,
        "status": status,
        "source_labels": source_labels,
        "tool_names": tool_names,
        "turn_ids": turn_ids,
        "tool_call_ids": tool_call_ids,
    }


def _normalized_context_state_payload(payload: object) -> Optional[dict]:
    normalized = _normalized_versioned_payload(payload)
    if normalized is None:
        return None
    normalized["constraints"] = [
        str(item).strip()
        for item in list(normalized.get("constraints") or [])
        if str(item).strip()
    ]
    normalized["open_questions"] = [
        str(item).strip()
        for item in list(normalized.get("open_questions") or [])
        if str(item).strip()
    ]
    normalized["resolved_facts"] = [
        str(item).strip()
        for item in list(normalized.get("resolved_facts") or [])
        if str(item).strip()
    ]
    normalized["evidence_ledger"] = [
        item
        for item in (_normalized_evidence_entry(raw) for raw in list(normalized.get("evidence_ledger") or []))
        if item is not None
    ]
    return normalized


def _normalized_replacement_history_entry(payload: object) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    role = str(payload.get("role") or "system").strip().lower()
    if role not in {"system", "user", "assistant"}:
        role = "system"
    content = str(payload.get("content") or "").strip()
    if not content:
        return None
    return {"role": role, "content": content}


def _normalized_compacted_history_payload(payload: object) -> Optional[dict]:
    normalized = _normalized_versioned_payload(payload)
    if normalized is None:
        return None
    normalized["replacement_history"] = [
        item
        for item in (
            _normalized_replacement_history_entry(raw)
            for raw in list(normalized.get("replacement_history") or [])
        )
        if item is not None
    ]
    return normalized


def _normalized_chat_preferences_payload(payload: object) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    language = str(payload.get("response_language") or "auto").strip()
    if language not in {"auto", "zh-CN", "en-US"}:
        language = "auto"
    verbosity = str(payload.get("response_verbosity") or "balanced").strip()
    if verbosity not in {"concise", "balanced", "detailed"}:
        verbosity = "balanced"
    web_search = str(payload.get("web_search") or "ask").strip()
    if web_search not in {"ask", "avoid", "allow_when_needed"}:
        web_search = "ask"
    return {
        "version": str(payload.get("version") or "chat_preferences.v1"),
        "response_language": language,
        "response_verbosity": verbosity,
        "web_search": web_search,
        "updated_at": str(payload.get("updated_at") or "").strip() or None,
    }


def _normalized_chat_preference_candidate(payload: object) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    candidate_id = str(payload.get("candidate_id") or "").strip()
    key = str(payload.get("key") or "").strip()
    suggested_value = str(payload.get("suggested_value") or "").strip()
    if key not in {"response_language", "response_verbosity", "web_search"}:
        return None
    allowed_values = {
        "response_language": {"auto", "zh-CN", "en-US"},
        "response_verbosity": {"concise", "balanced", "detailed"},
        "web_search": {"ask", "avoid", "allow_when_needed"},
    }
    if suggested_value not in allowed_values[key]:
        return None
    return {
        "candidate_id": candidate_id or f"candidate:{key}:{suggested_value}",
        "key": key,
        "suggested_value": suggested_value,
        "reason": str(payload.get("reason") or "").strip(),
        "source_excerpt": str(payload.get("source_excerpt") or "").strip(),
        "source_kind": str(payload.get("source_kind") or "draft").strip() or "draft",
    }


def _normalized_chat_rag_overrides_payload(payload: object) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    if not bool(payload.get("enabled", False)):
        return None

    scope_mode = str(payload.get("scope_mode") or "all").strip()
    if scope_mode not in {"all", "knowledge_base", "document"}:
        scope_mode = "all"

    def _normalize_ids(values: object) -> List[int]:
        normalized: List[int] = []
        for item in list(values or []):
            try:
                value = int(item)
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in normalized:
                normalized.append(value)
        return normalized

    normalized = {
        "version": str(payload.get("version") or "chat_rag_overrides.v1").strip() or "chat_rag_overrides.v1",
        "enabled": True,
        "scope_mode": scope_mode,
        "knowledge_base_ids": _normalize_ids(payload.get("knowledge_base_ids")),
        "document_ids": _normalize_ids(payload.get("document_ids")),
    }
    for key in (
        "use_reranker",
        "use_hybrid",
        "use_query_rewrite",
        "use_contextual_compression",
    ):
        if key in payload and payload.get(key) is not None:
            normalized[key] = bool(payload.get(key))
    return normalized


def _serialize_routing_decision(decision: object) -> Optional[dict]:
    if decision is None:
        return None
    if isinstance(decision, dict):
        return dict(decision)
    return {
        "intent": str(getattr(decision, "intent", "") or "").strip() or "general_chat",
        "intent_user_text": str(getattr(decision, "intent_user_text", "") or "").strip(),
        "carry_over_previous_goal": bool(getattr(decision, "carry_over_previous_goal", False)),
        "needs_tools": getattr(decision, "needs_tools", None),
        "confidence": float(getattr(decision, "confidence", 0.0) or 0.0),
        "reason": str(getattr(decision, "reason", "") or "").strip(),
        "source": str(getattr(decision, "source", "") or "").strip() or "llm",
        "latest_user_text": str(getattr(decision, "latest_user_text", "") or "").strip(),
    }


def message_to_response(msg: Message) -> MessageResponse:
    """将 Message 模型转换为 MessageResponse，处理 Enum 类型"""
    return MessageResponse(
        id=msg.id,
        conversation_id=msg.conversation_id,
        role=msg.role.value if hasattr(msg.role, 'value') else str(msg.role),
        content=msg.content,
        message_type=msg.message_type.value if hasattr(msg.message_type, 'value') else str(msg.message_type),
        thought=msg.thought,
        metadata=_sanitized_chat_message_response_metadata(msg.metadata_),
        prompt_tokens=msg.prompt_tokens or 0,
        completion_tokens=msg.completion_tokens or 0,
        total_tokens=msg.total_tokens or 0,
        created_at=msg.created_at,
    )


def _parse_message_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return datetime.utcnow()
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except Exception:
        return datetime.utcnow()


def _item_entry_role(kind: str, role: Optional[str]) -> Optional[str]:
    normalized_role = str(role or "").strip().lower()
    if normalized_role in {"user", "assistant", "system"}:
        return normalized_role
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind == "user_message":
        return "user"
    if normalized_kind in {"assistant_message", "stopped_assistant_message"}:
        return "assistant"
    if normalized_kind == "system_message":
        return "system"
    return None


def _project_message_from_item_entry(
    *,
    conversation_id: int,
    entry: object,
) -> Optional[MessageResponse]:
    if not isinstance(entry, dict):
        return None
    kind = str(entry.get("kind") or "").strip().lower()
    if kind not in {"message", "user_message", "assistant_message", "stopped_assistant_message", "system_message"}:
        return None
    try:
        message_id = int(entry["message_id"])
    except Exception:
        return None
    role = _item_entry_role(kind, entry.get("role"))
    if role not in {"user", "assistant", "system"}:
        return None
    return MessageResponse(
        id=message_id,
        conversation_id=conversation_id,
        role=role,
        content=str(entry.get("content") or ""),
        message_type="text",
        thought=str(entry.get("thought") or "").strip() or None,
        metadata=_sanitized_chat_message_response_metadata(entry.get("metadata")),
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        created_at=_parse_message_datetime(entry.get("created_at")),
    )


def _project_messages_from_item_stream(
    *,
    conversation_id: int,
    item_stream_payload: Optional[dict],
    skip: int = 0,
    limit: Optional[int] = None,
) -> List[MessageResponse]:
    if not isinstance(item_stream_payload, dict):
        return []
    store = ConversationItemStreamStore.from_payload(item_stream_payload)
    canonical_entry_ids = {entry.item_id for entry in store.canonical_message_entries()}
    entries = [
        raw
        for raw in list(item_stream_payload.get("entries") or [])
        if isinstance(raw, dict) and str(raw.get("item_id") or "").strip() in canonical_entry_ids
    ]
    projected = [
        item
        for item in (
            _project_message_from_item_entry(conversation_id=conversation_id, entry=raw)
            for raw in entries
        )
        if item is not None
    ]
    if skip > 0:
        projected = projected[skip:]
    if limit is not None:
        projected = projected[: max(int(limit), 0)]
    return projected


async def _raise_if_conversation_has_legacy_messages_without_item_stream(
    *,
    conversation_id: int,
    db: AsyncSession,
) -> None:
    result = await db.execute(
        select(func.count(Message.id)).where(Message.conversation_id == conversation_id)
    )
    message_count = int(result.scalar() or 0)
    if message_count <= 0:
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "conversation_item_stream_missing",
            "message": "对话仍停留在旧 message 存储格式，当前 chat 读取链只支持 item stream。请先执行回填后重试。",
            "conversation_id": conversation_id,
            "message_count": message_count,
        },
    )


def conversation_context_state_from_metadata(metadata: object) -> Optional[dict]:
    if not isinstance(metadata, dict):
        return None
    return _normalized_context_state_payload(metadata.get("context_state"))


def conversation_compacted_history_from_metadata(metadata: object) -> Optional[dict]:
    if not isinstance(metadata, dict):
        return None
    return _normalized_compacted_history_payload(metadata.get("compacted_history"))


def conversation_history_log_from_metadata(metadata: object) -> Optional[dict]:
    if not isinstance(metadata, dict):
        return None
    return _normalized_versioned_payload(metadata.get("history_log"))


def _normalized_turn_entry_payload(payload: object) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    turn_id = str(payload.get("turn_id") or "").strip()
    if not turn_id:
        return None
    try:
        user_message_id = int(payload["user_message_id"]) if payload.get("user_message_id") is not None else None
    except Exception:
        user_message_id = None
    try:
        assistant_message_id = int(payload["assistant_message_id"]) if payload.get("assistant_message_id") is not None else None
    except Exception:
        assistant_message_id = None
    try:
        iteration_count = max(int(payload.get("iteration_count") or 0), 0)
    except Exception:
        iteration_count = 0
    try:
        tool_call_count = max(int(payload.get("tool_call_count") or 0), 0)
    except Exception:
        tool_call_count = 0
    try:
        tool_result_count = max(int(payload.get("tool_result_count") or 0), 0)
    except Exception:
        tool_result_count = 0
    return {
        "turn_id": turn_id,
        "status": str(payload.get("status") or "running").strip().lower() or "running",
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "run_id": _normalized_optional_text(payload.get("run_id")),
        "user_content": _normalized_optional_text(payload.get("user_content")),
        "assistant_summary": _normalized_optional_text(payload.get("assistant_summary")),
        "iteration_count": iteration_count,
        "tool_call_count": tool_call_count,
        "tool_result_count": tool_result_count,
        "error_message": _normalized_optional_text(payload.get("error_message")),
        "started_at": _normalized_optional_text(payload.get("started_at")),
        "completed_at": _normalized_optional_text(payload.get("completed_at")),
    }


def _normalized_turn_store_payload(payload: object) -> Optional[dict]:
    normalized = _normalized_versioned_payload(payload)
    if normalized is None:
        return None
    normalized["entries"] = [
        item
        for item in (_normalized_turn_entry_payload(raw) for raw in list(normalized.get("entries") or []))
        if item is not None
    ]
    return normalized


def conversation_turn_store_from_metadata(metadata: object) -> Optional[dict]:
    if not isinstance(metadata, dict):
        return None
    return _normalized_turn_store_payload(metadata.get("turn_store"))


def conversation_tool_ledger_from_metadata(metadata: object) -> Optional[dict]:
    if not isinstance(metadata, dict):
        return None
    return _normalized_versioned_payload(metadata.get("tool_ledger"))


def conversation_context_snapshots_from_metadata(metadata: object) -> List[dict]:
    if not isinstance(metadata, dict):
        return []
    payload = metadata.get("context_snapshots")
    if not isinstance(payload, list):
        return []
    snapshots: List[dict] = []
    for item in payload:
        normalized = _normalized_context_snapshot_payload(item)
        if normalized is not None:
            snapshots.append(normalized)
    return snapshots


def _normalized_item_stream_entry_payload(payload: object) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    kind = str(payload.get("kind") or "").strip()
    if not kind:
        return None
    role = str(payload.get("role") or "").strip().lower() or None
    if role not in {None, "user", "assistant", "system", "tool"}:
        role = None
    try:
        iteration = max(int(payload.get("iteration") or 0), 0)
    except Exception:
        iteration = 0
    try:
        message_id = int(payload["message_id"]) if payload.get("message_id") is not None else None
    except Exception:
        message_id = None
    try:
        execution_time_ms = float(payload["execution_time_ms"]) if payload.get("execution_time_ms") is not None else None
    except Exception:
        execution_time_ms = None
    try:
        output_tokens_estimate = int(payload["output_tokens_estimate"]) if payload.get("output_tokens_estimate") is not None else None
    except Exception:
        output_tokens_estimate = None
    normalized: Dict[str, Any] = {
        "item_id": str(payload.get("item_id") or "").strip() or f"legacy-{kind}",
        "kind": kind,
        "turn_id": str(payload.get("turn_id") or "").strip() or None,
        "role": role,
        "content": str(payload.get("content") or "").strip() or None,
        "message_id": message_id,
        "run_id": str(payload.get("run_id") or "").strip() or None,
        "iteration": iteration,
        "tool_name": str(payload.get("tool_name") or "").strip() or None,
        "tool_call_id": str(payload.get("tool_call_id") or "").strip() or None,
        "status": str(payload.get("status") or "").strip() or None,
        "arguments": dict(payload.get("arguments") or {}) if isinstance(payload.get("arguments"), dict) else None,
        "thought": str(payload.get("thought") or "").strip() or None,
        "summary": str(payload.get("summary") or "").strip() or None,
        "success": bool(payload.get("success")) if payload.get("success") is not None else None,
        "error": str(payload.get("error") or "").strip() or None,
        "permission_required": bool(payload.get("permission_required")),
        "execution_time_ms": execution_time_ms,
        "output_tokens_estimate": output_tokens_estimate,
        "truncated": bool(payload.get("truncated")) if payload.get("truncated") is not None else None,
        "parallel_group": str(payload.get("parallel_group") or "").strip() or None,
        "metadata": dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else None,
        "created_at": str(payload.get("created_at") or "").strip() or None,
    }
    return normalized


def _normalized_item_stream_payload(payload: object) -> Optional[dict]:
    normalized = _normalized_versioned_payload(payload)
    if normalized is None:
        return None
    normalized["entries"] = [
        item
        for item in (
            _normalized_item_stream_entry_payload(raw)
            for raw in list(normalized.get("entries") or [])
        )
        if item is not None
    ]
    return normalized


def conversation_item_stream_from_metadata(metadata: object) -> Optional[dict]:
    if not isinstance(metadata, dict):
        return None
    return _normalized_item_stream_payload(metadata.get("item_stream"))


async def append_message_item_entry(
    *,
    conversation_id: int,
    role: str,
    content: str,
    turn_id: Optional[str] = None,
    message_id: Optional[int] = None,
    created_at: Optional[datetime] = None,
    thought: Optional[str] = None,
    metadata: Optional[dict] = None,
    kind: str = "message",
) -> None:
    runtime_service = get_agent_runtime_service()
    await runtime_service.append_conversation_item_entries(
        conversation_id,
        [
            {
                "kind": kind,
                "turn_id": turn_id,
                "role": role,
                "content": content,
                "message_id": message_id,
                "created_at": created_at.isoformat() if created_at else None,
                "thought": thought,
                "metadata": metadata or {},
            }
        ],
    )


def _assistant_summary_text(content: str, *, fallback: Optional[str] = None, limit: int = 160) -> Optional[str]:
    text = " ".join(str(content or "").split()).strip()
    if not text:
        text = " ".join(str(fallback or "").split()).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)].rstrip() + "…"


def _sanitized_persisted_chat_metadata(payload: object) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    metadata: dict[str, Any] = {}
    rag_metrics = payload.get("rag_metrics")
    if isinstance(rag_metrics, dict):
        metadata["rag_metrics"] = dict(rag_metrics)
    return metadata or None


def _sanitized_chat_message_response_metadata(payload: object) -> Optional[dict]:
    """Expose only stable chat UI metadata from persisted messages."""
    return _sanitized_persisted_chat_metadata(payload)


@router.get("/conversations", response_model=List[ConversationListResponse])
async def list_conversations(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    archived: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取对话列表"""
    # 子查询：获取每个对话的最后一条消息
    last_message_subq = (
        select(
            Message.conversation_id,
            Message.content.label("last_message"),
            func.row_number().over(
                partition_by=Message.conversation_id,
                order_by=desc(Message.created_at)
            ).label("rn")
        )
        .where(Message.role == MessageRole.ASSISTANT)
        .subquery()
    )
    
    # 子查询：消息计数
    message_count_subq = (
        select(
            Message.conversation_id,
            func.count(Message.id).label("message_count")
        )
        .group_by(Message.conversation_id)
        .subquery()
    )
    
    # 主查询
    query = (
        select(
            Conversation,
            last_message_subq.c.last_message,
            func.coalesce(message_count_subq.c.message_count, 0).label("message_count")
        )
        .outerjoin(
            last_message_subq,
            (Conversation.id == last_message_subq.c.conversation_id) & 
            (last_message_subq.c.rn == 1)
        )
        .outerjoin(
            message_count_subq,
            Conversation.id == message_count_subq.c.conversation_id
        )
        .where(
            Conversation.user_id == current_user.id,
            Conversation.is_archived == (1 if archived else 0)
        )
        .order_by(desc(Conversation.updated_at))
        .offset(skip)
        .limit(limit)
    )
    
    result = await db.execute(query)
    rows = result.all()
    
    conversations = []
    for row in rows:
        conv = row[0]
        conv_dict = {
            "id": conv.id,
            "title": conv.title,
            "llm_provider": conv.llm_provider,
            "is_archived": conv.is_archived,
            "created_at": conv.created_at,
            "updated_at": conv.updated_at,
            "last_message": row[1][:100] if row[1] else None,
            "message_count": row[2] or 0
        }
        conversations.append(ConversationListResponse(**conv_dict))
    
    return conversations


@router.get("/messages/search")
async def search_messages(
    q: str = Query(..., min_length=1, max_length=200, description="搜索关键词"),
    limit: int = Query(20, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """搜索用户的历史消息"""
    search_term = f"%{q}%"
    
    # 搜索消息内容，同时获取对话标题
    query = (
        select(
            Message.id,
            Message.conversation_id,
            Message.role,
            Message.content,
            Message.created_at,
            Conversation.title.label("conversation_title")
        )
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.user_id == current_user.id,
            Conversation.is_archived == 0,
            Message.content.ilike(search_term)
        )
        .order_by(desc(Message.created_at))
        .limit(limit)
    )
    
    result = await db.execute(query)
    rows = result.all()
    
    results = []
    for row in rows:
        # 找到匹配文本的位置，提取上下文片段
        content = row.content
        q_lower = q.lower()
        content_lower = content.lower()
        match_pos = content_lower.find(q_lower)
        
        # 提取匹配位置周围的文本片段（前后各50个字符）
        start = max(0, match_pos - 50)
        end = min(len(content), match_pos + len(q) + 50)
        snippet = content[start:end]
        if start > 0:
            snippet = "..." + snippet
        if end < len(content):
            snippet = snippet + "..."
        
        results.append({
            "message_id": row.id,
            "conversation_id": row.conversation_id,
            "conversation_title": row.conversation_title or "新对话",
            "role": row.role.value if hasattr(row.role, 'value') else str(row.role),
            "content_snippet": snippet,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        })
    
    return {
        "query": q,
        "total": len(results),
        "results": results
    }


@router.post("/conversations", response_model=ConversationResponse)
async def create_conversation(
    data: ConversationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """创建新对话"""
    conversation = Conversation(
        user_id=current_user.id,
        title=data.title,
        llm_provider=data.llm_provider or current_user.preferred_llm_provider,
    )
    
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    
    logger.info(f"创建对话: {conversation.id} by {current_user.username}")
    
    # 手动构建响应，避免触发懒加载
    return ConversationResponse(
        id=conversation.id,
        user_id=conversation.user_id,
        title=conversation.title,
        llm_provider=conversation.llm_provider,
        llm_model=conversation.llm_model,
        is_archived=conversation.is_archived,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        context_state=conversation_context_state_from_metadata(conversation.metadata_),
        compacted_history=conversation_compacted_history_from_metadata(conversation.metadata_),
        history_log=conversation_history_log_from_metadata(conversation.metadata_),
        turn_store=conversation_turn_store_from_metadata(conversation.metadata_),
        tool_ledger=conversation_tool_ledger_from_metadata(conversation.metadata_),
        item_stream=conversation_item_stream_from_metadata(conversation.metadata_),
        context_snapshots=conversation_context_snapshots_from_metadata(conversation.metadata_),
        messages=[]  # 新建对话没有消息
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取对话详情"""
    result = await db.execute(
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id
        )
    )
    conversation = result.scalar_one_or_none()
    
    if not conversation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="对话不存在"
        )
    
    # 手动构建响应
    item_stream = conversation_item_stream_from_metadata(conversation.metadata_)
    if item_stream is None:
        await _raise_if_conversation_has_legacy_messages_without_item_stream(
            conversation_id=conversation.id,
            db=db,
        )
    projected_messages = _project_messages_from_item_stream(
        conversation_id=conversation.id,
        item_stream_payload=item_stream,
    )

    return ConversationResponse(
        id=conversation.id,
        user_id=conversation.user_id,
        title=conversation.title,
        llm_provider=conversation.llm_provider,
        llm_model=conversation.llm_model,
        is_archived=conversation.is_archived,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        context_state=conversation_context_state_from_metadata(conversation.metadata_),
        compacted_history=conversation_compacted_history_from_metadata(conversation.metadata_),
        history_log=conversation_history_log_from_metadata(conversation.metadata_),
        turn_store=conversation_turn_store_from_metadata(conversation.metadata_),
        tool_ledger=conversation_tool_ledger_from_metadata(conversation.metadata_),
        item_stream=item_stream,
        context_snapshots=conversation_context_snapshots_from_metadata(conversation.metadata_),
        messages=projected_messages,
    )


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """删除对话"""
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id
        )
    )
    conversation = result.scalar_one_or_none()
    
    if not conversation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="对话不存在"
        )
    
    await db.delete(conversation)
    await db.commit()
    
    logger.info(f"删除对话: {conversation_id}")
    
    return {"message": "删除成功"}


@router.put("/conversations/{conversation_id}/archive")
async def archive_conversation(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """归档对话"""
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id
        )
    )
    conversation = result.scalar_one_or_none()
    
    if not conversation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="对话不存在"
        )
    
    conversation.is_archived = 1 if conversation.is_archived == 0 else 0
    await db.commit()
    
    return {"message": "操作成功", "is_archived": conversation.is_archived}


@router.post("/conversations/{conversation_id}/compact", response_model=ConversationCompactResponse)
async def compact_conversation(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """手动压缩对话上下文，立即刷新会话级状态与持久历史层。"""
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id,
        )
    )
    conversation = result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="对话不存在",
        )

    service = get_conversation_context_compaction_service()
    try:
        artifacts = await service.compact_now(conversation_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="对话不存在",
        ) from None
    except ConversationItemStreamUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "conversation_item_stream_missing",
                "message": "对话尚未建立 item stream，当前不能执行压缩。请先在该对话中发送一条新消息后重试。",
                "conversation_id": conversation_id,
            },
        ) from None

    runtime_service = get_agent_runtime_service()
    context_state = _normalized_context_state_payload(
        await runtime_service.get_conversation_context_state(conversation_id)
    )
    compacted_history = _normalized_compacted_history_payload(
        await runtime_service.get_conversation_compacted_history(conversation_id)
    )
    history_log = _normalized_versioned_payload(
        await runtime_service.get_conversation_history_log(conversation_id)
    )
    turn_store = _normalized_turn_store_payload(
        await runtime_service.get_conversation_turn_store(conversation_id)
    )
    tool_ledger = _normalized_versioned_payload(
        await runtime_service.get_conversation_tool_ledger(conversation_id)
    )
    item_stream = _normalized_item_stream_payload(
        await runtime_service.get_conversation_item_stream(conversation_id)
    )
    context_snapshots = [
        item
        for item in (
            _normalized_context_snapshot_payload(raw)
            for raw in await runtime_service.get_conversation_context_snapshots(conversation_id)
        )
        if item is not None
    ]

    return ConversationCompactResponse(
        conversation_id=conversation_id,
        context_state=context_state,
        compacted_history=compacted_history,
        history_log=history_log,
        turn_store=turn_store,
        tool_ledger=tool_ledger,
        item_stream=item_stream,
        context_snapshots=context_snapshots,
        summary_text=artifacts.summary_text or None,
        compacted_message_count=artifacts.compacted_message_count,
    )


@router.post("/context-preview", response_model=ChatContextPreviewResponse)
async def preview_chat_context(
    request: ChatContextPreviewRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """预览下一条消息真正会送入模型的上下文。"""
    conversation_id = request.conversation_id
    conversation = None

    if conversation_id:
        result = await db.execute(
            select(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.user_id == current_user.id,
            )
        )
        conversation = result.scalar_one_or_none()
        if not conversation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="对话不存在",
            )

    messages: List[Dict[str, Any]] = [{"role": "user", "content": request.message}]

    llm_provider = request.llm_provider or (conversation.llm_provider if conversation else current_user.preferred_llm_provider)
    llm_service = LLMService(llm_provider)
    runtime_service = get_agent_runtime_service()

    from app.core.database import async_session_factory
    from app.services.react_agent import AgentRuntimeContext, create_chat_preview_planner

    tool_registry = get_tool_registry(
        db=None,
        user_id=current_user.id,
        db_session_factory=async_session_factory,
        route_profile="chat",
        initialize_mcp=False,
    )
    planner = create_chat_preview_planner(
        llm_service,
        tool_registry,
        runtime_context=AgentRuntimeContext(
            user_id=current_user.id,
            channel="chat",
            conversation_id=conversation_id,
            chat_preferences_override=runtime_service.normalize_chat_preference_overrides(
                request.chat_preference_overrides
            ),
            rag_overrides=runtime_service.normalize_chat_rag_overrides(request.rag_overrides),
        ),
    )
    if request.use_tools is False:
        direct_response = await planner.prepare_direct_response(messages, force_no_tools=True)
        assert direct_response is not None
        prepared = SimpleNamespace(
            preview_mode="direct",
            system_prompt=direct_response.system_prompt,
            llm_messages=direct_response.llm_messages,
            routing_decision=direct_response.routing_decision,
            context=direct_response.context,
        )
    else:
        prepared = await planner.prepare_context_preview(messages)
    conversation_revision = await runtime_service.get_conversation_revision(conversation_id)
    chat_preferences = await runtime_service.get_user_chat_preferences(user_id=current_user.id)
    effective_chat_preferences = runtime_service.merge_chat_preferences(
        chat_preferences,
        request.chat_preference_overrides,
    )
    effective_rag_overrides = runtime_service.normalize_chat_rag_overrides(request.rag_overrides)
    chat_preference_candidates = runtime_service.extract_chat_preference_candidates(
        draft_message=request.message,
        confirmed_preferences=chat_preferences,
    )
    send_plan = runtime_service.store_prepared_send_plan(
        user_id=current_user.id,
        conversation_id=conversation_id,
        llm_provider=llm_provider,
        draft_message=request.message,
        preview_mode=prepared.preview_mode,
        conversation_revision=conversation_revision,
        system_prompt=prepared.system_prompt,
        llm_messages=prepared.llm_messages,
        routing_decision=_serialize_routing_decision(prepared.routing_decision),
        tool_selection=getattr(planner, "_last_tool_selection", {}),
        chat_preferences=effective_chat_preferences,
        rag_overrides=effective_rag_overrides,
        conversation_state=prepared.context.conversation_state,
        compacted_history=prepared.context.compacted_history,
    )
    context_state = (
        _normalized_context_state_payload(await runtime_service.get_conversation_context_state(conversation_id))
        if conversation_id is not None
        else None
    )
    compacted_history = (
        _normalized_compacted_history_payload(await runtime_service.get_conversation_compacted_history(conversation_id))
        if conversation_id is not None
        else None
    )
    history_log = (
        _normalized_versioned_payload(await runtime_service.get_conversation_history_log(conversation_id))
        if conversation_id is not None
        else None
    )
    turn_store = (
        _normalized_turn_store_payload(await runtime_service.get_conversation_turn_store(conversation_id))
        if conversation_id is not None
        else None
    )
    tool_ledger = (
        _normalized_versioned_payload(await runtime_service.get_conversation_tool_ledger(conversation_id))
        if conversation_id is not None
        else None
    )
    item_stream = (
        _normalized_item_stream_payload(await runtime_service.get_conversation_item_stream(conversation_id))
        if conversation_id is not None
        else None
    )
    context_snapshots = (
        [
            item
            for item in (
                _normalized_context_snapshot_payload(raw)
                for raw in await runtime_service.get_conversation_context_snapshots(conversation_id)
            )
            if item is not None
        ]
        if conversation_id is not None
        else []
    )

    return ChatContextPreviewResponse(
        conversation_id=conversation_id,
        preview_mode=prepared.preview_mode,
        context_debug=dict(prepared.context.context_debug or {}),
        context_state=context_state,
        compacted_history=compacted_history,
        history_log=history_log,
        turn_store=turn_store,
        tool_ledger=tool_ledger,
        item_stream=item_stream,
        context_snapshots=context_snapshots,
        chat_preferences=_normalized_chat_preferences_payload(chat_preferences),
        effective_chat_preferences=_normalized_chat_preferences_payload(effective_chat_preferences),
        effective_rag_overrides=_normalized_chat_rag_overrides_payload(effective_rag_overrides),
        chat_preference_candidates=[
            item
            for item in (
                _normalized_chat_preference_candidate(raw)
                for raw in list(chat_preference_candidates or [])
            )
            if item is not None
        ],
        send_plan=send_plan,
    )


@router.post("/send")
async def send_message(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """发送消息（支持流式响应和工具调用）"""
    # 获取或创建对话
    if request.conversation_id:
        result = await db.execute(
            select(Conversation)
            .where(
                Conversation.id == request.conversation_id,
                Conversation.user_id == current_user.id
            )
        )
        conversation = result.scalar_one_or_none()
        
        if not conversation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="对话不存在"
            )
    else:
        # 创建新对话
        conversation = Conversation(
            user_id=current_user.id,
            title=request.message[:50] + "..." if len(request.message) > 50 else request.message,
            llm_provider=request.llm_provider or current_user.preferred_llm_provider,
        )
        db.add(conversation)
        await db.commit()
        await db.refresh(conversation)
    
    runtime_service = get_agent_runtime_service()
    pre_send_conversation_revision = await runtime_service.get_conversation_revision(
        request.conversation_id if request.conversation_id is not None else None
    )
    prepared_send_plan = runtime_service.take_prepared_send_plan(
        plan_id=request.send_plan_id,
        user_id=current_user.id,
        conversation_id=request.conversation_id,
        draft_message=request.message,
        llm_provider=request.llm_provider or conversation.llm_provider,
        conversation_revision=pre_send_conversation_revision,
    )

    # 保存用户消息
    if request.conversation_id:
        existing_message_count_result = await db.execute(
            select(func.count(Message.id)).where(Message.conversation_id == int(request.conversation_id))
        )
        existing_message_count = int(existing_message_count_result.scalar() or 0)
    else:
        existing_message_count = 0
    is_first_turn = existing_message_count == 0
    user_message = Message(
        conversation_id=conversation.id,
        role=MessageRole.USER,
        content=request.message,
        message_type=MessageType.TEXT,
    )
    db.add(user_message)
    await db.commit()
    await db.refresh(user_message)
    turn_id = f"turn:{user_message.id}"
    await append_message_item_entry(
        conversation_id=conversation.id,
        role="user",
        content=user_message.content,
        turn_id=turn_id,
        message_id=user_message.id,
        created_at=user_message.created_at,
        kind="user_message",
    )
    await runtime_service.upsert_conversation_turn_entry(
        conversation.id,
        {
            "turn_id": turn_id,
            "status": "running",
            "user_message_id": user_message.id,
            "user_content": user_message.content,
            "started_at": user_message.created_at.isoformat() if user_message.created_at else datetime.utcnow().isoformat(),
        },
    )
    
    logger.info(f"用户消息: conv={conversation.id}, msg={user_message.id}")
    
    # 获取 LLM 服务
    llm_provider = request.llm_provider or conversation.llm_provider
    llm_service = LLMService(llm_provider)
    
    agent_messages = [{"role": "user", "content": request.message}]
    
    # 保存对话ID用于流式响应
    conversation_id = conversation.id
    
    # 是否使用工具 - 默认启用，除非明确禁用
    use_tools = True
    if hasattr(request, 'use_tools') and request.use_tools is not None:
        use_tools = request.use_tools
    
    logger.info(f"对话 {conversation_id}: use_tools={use_tools}")

    from app.core.database import async_session_factory
    from app.services.react_agent import (
        AgentRuntimeContext,
        create_chat_preview_planner,
        create_react_agent,
    )

    def _build_runtime_context() -> AgentRuntimeContext:
        effective_chat_preferences = (
            prepared_send_plan.get("chat_preferences")
            if isinstance(prepared_send_plan, dict)
            else request.chat_preference_overrides
        )
        effective_rag_overrides = (
            prepared_send_plan.get("rag_overrides")
            if isinstance(prepared_send_plan, dict)
            else request.rag_overrides
        )
        return AgentRuntimeContext(
            user_id=current_user.id,
            channel="chat",
            conversation_id=conversation_id,
            turn_id=turn_id,
            chat_preferences_override=runtime_service.normalize_chat_preference_overrides(effective_chat_preferences),
            rag_overrides=runtime_service.normalize_chat_rag_overrides(effective_rag_overrides),
        )

    def _create_chat_agent():
        tool_registry = get_tool_registry(
            db=None,
            user_id=current_user.id,
            db_session_factory=async_session_factory,
            route_profile="chat",
        )
        return create_react_agent(
            llm_service,
            tool_registry,
            max_iterations=settings.react_max_iterations,
            runtime_context=_build_runtime_context(),
        )

    def _create_direct_planner():
        tool_registry = get_tool_registry(
            db=None,
            user_id=current_user.id,
            db_session_factory=async_session_factory,
            route_profile="chat",
            initialize_mcp=False,
        )
        return create_chat_preview_planner(
            llm_service,
            tool_registry,
            runtime_context=_build_runtime_context(),
        )
    
    if request.stream:
        # 流式响应
        async def generate():
            full_content = ""
            thought = ""
            rag_metrics = None
            context_debug = None
            reasoning_summary = None
            current_iteration = 0

            def _phase_payload(key: str, *, first_turn: bool) -> dict:
                if key == "loading_context":
                    return {
                        "key": key,
                        "label": "正在准备本轮请求…",
                        "hint": "正在读取当前会话状态和必要历史。",
                    } if not first_turn else {
                        "key": key,
                        "label": "正在准备本轮请求…",
                        "hint": "首轮不会整理很多历史，正在建立这次请求。",
                    }
                if key == "routing":
                    return {
                        "key": key,
                        "label": "正在判断回答路径…",
                        "hint": "正在判断这轮是否需要工具，还是可以直接回答。",
                    }
                if key == "waiting_model":
                    return {
                        "key": key,
                        "label": "回答路径已确定，正在等待模型开始回答…",
                        "hint": "请求已经发给主模型，通常很快会有首个结果。",
                    }
                return {
                    "key": key,
                    "label": "正在处理中…",
                    "hint": "",
                }

            async def _load_turn_payload() -> Optional[dict]:
                return _normalized_turn_store_payload(
                    await runtime_service.get_conversation_turn_store(conversation_id)
                )

            async def _finalize_turn(
                *,
                status_value: str,
                assistant_message_id: Optional[int] = None,
                assistant_content: str = "",
                assistant_thought: Optional[str] = None,
                run_id: Optional[str] = None,
                iteration_count: Optional[int] = None,
                error_message: Optional[str] = None,
            ) -> Optional[dict]:
                await runtime_service.upsert_conversation_turn_entry(
                    conversation_id,
                    {
                        "turn_id": turn_id,
                        "status": status_value,
                        "assistant_message_id": assistant_message_id,
                        "assistant_summary": _assistant_summary_text(
                            assistant_content,
                            fallback=assistant_thought,
                        ),
                        "run_id": run_id,
                        "iteration_count": iteration_count,
                        "error_message": error_message,
                        "completed_at": datetime.utcnow().isoformat(),
                    },
                )
                return await _load_turn_payload()

            async def _append_reasoning_item(
                *,
                summary_text: Optional[str],
                message_id: Optional[int] = None,
                run_id: Optional[str] = None,
                iteration_count: Optional[int] = None,
                created_at: Optional[datetime] = None,
            ) -> None:
                compacted = _assistant_summary_text(summary_text or "", limit=240)
                if not compacted:
                    return
                await runtime_service.append_conversation_item_entries(
                    conversation_id,
                    [
                        {
                            "kind": "reasoning_summary",
                            "turn_id": turn_id,
                            "role": "assistant",
                            "message_id": message_id,
                            "run_id": run_id,
                            "iteration": max(int(iteration_count or 0), 0),
                            "summary": compacted,
                            "content": compacted,
                            "created_at": created_at.isoformat() if created_at else datetime.utcnow().isoformat(),
                        }
                    ],
                )
            
            try:
                # 发送开始事件
                yield f"data: {json.dumps({'event': 'start', 'data': {'conversation_id': conversation_id, 'message_id': user_message.id, 'turn_id': turn_id}})}\n\n"
                
                if use_tools:
                    # 先走轻量 planner 判断是否可以直答；只有真的需要工具时才初始化完整 agent。
                    planner = None
                    direct_response = None
                    if prepared_send_plan and str(prepared_send_plan.get("preview_mode") or "") == "direct":
                        logger.info(f"[Chat] 复用完整预演 send_plan: conv={conversation_id}")
                    else:
                        yield f"data: {json.dumps({'event': 'phase', 'data': _phase_payload('loading_context', first_turn=is_first_turn)})}\n\n"
                        yield f"data: {json.dumps({'event': 'phase', 'data': _phase_payload('routing', first_turn=is_first_turn)})}\n\n"
                        planner = _create_direct_planner()
                        direct_response = await planner.prepare_direct_response(agent_messages)
                    if prepared_send_plan and str(prepared_send_plan.get("preview_mode") or "") == "direct":
                        logger.info(
                            f"[Chat] 复用直连流式回答 send_plan: conv={conversation_id}, "
                            f"intent={((prepared_send_plan.get('routing_decision') or {}).get('intent') or 'unknown')}"
                        )
                        yield f"data: {json.dumps({'event': 'phase', 'data': _phase_payload('waiting_model', first_turn=is_first_turn)})}\n\n"
                        yield f"data: {json.dumps({'event': 'model_info', 'data': {'provider': getattr(llm_service, 'provider', ''), 'model': (getattr(llm_service, 'config', {}) or {}).get('model')}})}\n\n"
                        context_debug = None

                        async for chunk in llm_service.chat_stream(
                            messages=[dict(item) for item in list(prepared_send_plan.get("llm_messages") or []) if isinstance(item, dict)],
                            system_prompt=str(prepared_send_plan.get("system_prompt") or ""),
                            temperature=settings.react_temperature,
                            max_tokens=settings.llm_max_tokens,
                        ):
                            full_content += chunk
                            yield f"data: {json.dumps({'event': 'content', 'data': chunk})}\n\n"

                        logger.info(f"[Chat] 复用直连 send_plan 完成: content_len={len(full_content)}")
                        async with async_session_factory() as save_db:
                            message_metadata = _sanitized_persisted_chat_metadata({})
                            assistant_message = Message(
                                conversation_id=conversation_id,
                                role=MessageRole.ASSISTANT,
                                content=full_content,
                                message_type=MessageType.TEXT,
                                metadata_=message_metadata or None,
                            )
                            save_db.add(assistant_message)
                            await save_db.commit()
                            await save_db.refresh(assistant_message)
                            await append_message_item_entry(
                                conversation_id=conversation_id,
                                role="assistant",
                                content=full_content,
                                turn_id=turn_id,
                                message_id=assistant_message.id,
                                created_at=assistant_message.created_at,
                                thought=None,
                                metadata=message_metadata or None,
                                kind="assistant_message",
                            )
                            await _append_reasoning_item(
                                summary_text=None,
                                message_id=assistant_message.id,
                                created_at=assistant_message.created_at,
                            )
                            turn_store = await _finalize_turn(
                                status_value="completed",
                                assistant_message_id=assistant_message.id,
                                assistant_content=full_content,
                            )
                            get_conversation_context_compaction_service().enqueue_conversation(conversation_id)
                            conversation_context_state = await runtime_service.get_conversation_context_state(
                                conversation_id
                            )
                            conversation_tool_ledger = await runtime_service.get_conversation_tool_ledger(
                                conversation_id
                            )
                            conversation_turn_store = turn_store
                            conversation_item_stream = await runtime_service.get_conversation_item_stream(
                                conversation_id
                            )

                            done_payload = {
                                "message_id": assistant_message.id,
                                "answer": full_content,
                            }
                            if conversation_context_state:
                                done_payload["context_state"] = conversation_context_state
                            if conversation_turn_store:
                                done_payload["turn_store"] = conversation_turn_store
                            if conversation_tool_ledger:
                                done_payload["tool_ledger"] = conversation_tool_ledger
                            if conversation_item_stream:
                                done_payload["item_stream"] = conversation_item_stream

                            yield f"data: {json.dumps({'event': 'done', 'data': done_payload})}\n\n"
                        return
                    if direct_response is not None:
                        routing_intent = "unknown"
                        if isinstance(direct_response.routing_decision, dict):
                            routing_intent = str(direct_response.routing_decision.get("intent") or "unknown")
                        elif direct_response.routing_decision is not None:
                            routing_intent = str(getattr(direct_response.routing_decision, "intent", "unknown") or "unknown")
                        logger.info(
                            f"[Chat] 直连流式回答: conv={conversation_id}, "
                            f"intent={routing_intent}"
                        )
                        yield f"data: {json.dumps({'event': 'phase', 'data': _phase_payload('waiting_model', first_turn=is_first_turn)})}\n\n"
                        yield f"data: {json.dumps({'event': 'model_info', 'data': {'provider': getattr(llm_service, 'provider', ''), 'model': (getattr(llm_service, 'config', {}) or {}).get('model')}})}\n\n"
                        if isinstance(direct_response.context.context_debug, dict) and direct_response.context.context_debug:
                            context_debug = direct_response.context.context_debug
                            yield f"data: {json.dumps({'event': 'context_debug', 'data': context_debug})}\n\n"

                        async for chunk in llm_service.chat_stream(
                            messages=direct_response.llm_messages,
                            system_prompt=direct_response.system_prompt,
                            temperature=settings.react_temperature,
                            max_tokens=settings.llm_max_tokens,
                        ):
                            full_content += chunk
                            yield f"data: {json.dumps({'event': 'content', 'data': chunk})}\n\n"

                        logger.info(f"[Chat] 直连流式完成: content_len={len(full_content)}")
                        async with async_session_factory() as save_db:
                            message_metadata = _sanitized_persisted_chat_metadata({})
                            assistant_message = Message(
                                conversation_id=conversation_id,
                                role=MessageRole.ASSISTANT,
                                content=full_content,
                                message_type=MessageType.TEXT,
                                metadata_=message_metadata or None,
                            )
                            save_db.add(assistant_message)
                            await save_db.commit()
                            await save_db.refresh(assistant_message)
                            await append_message_item_entry(
                                conversation_id=conversation_id,
                                role="assistant",
                                content=full_content,
                                turn_id=turn_id,
                                message_id=assistant_message.id,
                                created_at=assistant_message.created_at,
                                thought=None,
                                metadata=message_metadata or None,
                                kind="assistant_message",
                            )
                            await _append_reasoning_item(
                                summary_text=None,
                                message_id=assistant_message.id,
                                created_at=assistant_message.created_at,
                            )
                            turn_store = await _finalize_turn(
                                status_value="completed",
                                assistant_message_id=assistant_message.id,
                                assistant_content=full_content,
                            )
                            get_conversation_context_compaction_service().enqueue_conversation(conversation_id)
                            conversation_context_state = await runtime_service.get_conversation_context_state(
                                conversation_id
                            )
                            conversation_tool_ledger = await runtime_service.get_conversation_tool_ledger(
                                conversation_id
                            )
                            conversation_turn_store = turn_store
                            conversation_item_stream = await runtime_service.get_conversation_item_stream(
                                conversation_id
                            )

                            done_payload = {
                                "message_id": assistant_message.id,
                                "answer": full_content,
                            }
                            if conversation_context_state:
                                done_payload["context_state"] = conversation_context_state
                            if conversation_turn_store:
                                done_payload["turn_store"] = conversation_turn_store
                            if conversation_tool_ledger:
                                done_payload["tool_ledger"] = conversation_tool_ledger
                            if conversation_item_stream:
                                done_payload["item_stream"] = conversation_item_stream

                            yield f"data: {json.dumps({'event': 'done', 'data': done_payload})}\n\n"
                        return

                    # 使用 ReAct Agent（带工具）
                    yield f"data: {json.dumps({'event': 'phase', 'data': _phase_payload('waiting_model', first_turn=is_first_turn)})}\n\n"
                    agent = _create_chat_agent()
                    
                    async for event in agent.run(agent_messages, stream=True, prepared_plan=prepared_send_plan):
                        event_type = event["type"]
                        event_data = event["data"]
                        
                        if event_type == "start":
                            yield f"data: {json.dumps({'event': 'model_info', 'data': event_data})}\n\n"
                        elif event_type == "thinking_start":
                            current_iteration += 1
                            yield f"data: {json.dumps({'event': 'thinking_start', 'data': {'iteration': current_iteration}})}\n\n"
                        elif event_type == "thinking":
                            yield f"data: {json.dumps({'event': 'thinking', 'data': event_data})}\n\n"
                        elif event_type == "thought":
                            thought = event_data
                            yield f"data: {json.dumps({'event': 'thought', 'data': event_data})}\n\n"
                        elif event_type == "action":
                            yield f"data: {json.dumps({'event': 'action', 'data': event_data})}\n\n"
                        elif event_type == "observation":
                            yield f"data: {json.dumps({'event': 'observation', 'data': event_data})}\n\n"
                        elif event_type == "context_debug":
                            if isinstance(event_data, dict):
                                context_debug = event_data
                            yield f"data: {json.dumps({'event': 'context_debug', 'data': event_data})}\n\n"
                        elif event_type == "content":
                            full_content += event_data
                            yield f"data: {json.dumps({'event': 'content', 'data': event_data})}\n\n"
                        elif event_type == "answer":
                            full_content = event_data
                            yield f"data: {json.dumps({'event': 'content', 'data': event_data})}\n\n"
                        elif event_type == "error":
                            logger.error(f"[Chat] ReAct Agent 错误: {event_data}")
                            yield f"data: {json.dumps({'event': 'error', 'data': event_data})}\n\n"
                        elif event_type == "done":
                            if isinstance(event_data, dict):
                                if event_data.get("thought"):
                                    thought = event_data["thought"]
                                if event_data.get("answer") and not full_content:
                                    full_content = event_data["answer"]
                                if isinstance(event_data.get("rag_metrics"), dict):
                                    rag_metrics = event_data["rag_metrics"]
                                if isinstance(event_data.get("reasoning_summary"), str):
                                    reasoning_summary = event_data["reasoning_summary"]
                            
                            logger.info(f"[Chat] 对话完成: iterations={current_iteration}, content_len={len(full_content)}")
                            
                            # 保存助手消息（包含完整的ReAct步骤）
                            async with async_session_factory() as save_db:
                                message_metadata: dict[str, Any] = {}
                                if isinstance(rag_metrics, dict):
                                    message_metadata["rag_metrics"] = rag_metrics
                                persisted_message_metadata = _sanitized_persisted_chat_metadata(message_metadata) or {}
                                assistant_message = Message(
                                    conversation_id=conversation_id,
                                    role=MessageRole.ASSISTANT,
                                    content=full_content,
                                    message_type=MessageType.TEXT,
                                    thought=(reasoning_summary or thought) if (reasoning_summary or thought) else None,
                                    metadata_=persisted_message_metadata or None,
                                )
                                save_db.add(assistant_message)
                                await save_db.commit()
                                await save_db.refresh(assistant_message)
                                await append_message_item_entry(
                                    conversation_id=conversation_id,
                                    role="assistant",
                                    content=full_content,
                                    turn_id=turn_id,
                                    message_id=assistant_message.id,
                                    created_at=assistant_message.created_at,
                                    thought=(reasoning_summary or thought) if (reasoning_summary or thought) else None,
                                    metadata=persisted_message_metadata or None,
                                    kind="assistant_message",
                                )
                                await _append_reasoning_item(
                                    summary_text=(reasoning_summary or thought),
                                    message_id=assistant_message.id,
                                    run_id=str(event_data.get("run_id") or "").strip() or None,
                                    iteration_count=current_iteration,
                                    created_at=assistant_message.created_at,
                                )
                                turn_store = await _finalize_turn(
                                    status_value="completed",
                                    assistant_message_id=assistant_message.id,
                                    assistant_content=full_content,
                                    assistant_thought=(reasoning_summary or thought),
                                    run_id=str(event_data.get("run_id") or "").strip() or None,
                                    iteration_count=current_iteration,
                                )
                                get_conversation_context_compaction_service().enqueue_conversation(conversation_id)
                                conversation_context_state = await agent.runtime_service.get_conversation_context_state(
                                    conversation_id
                                )
                                conversation_tool_ledger = await agent.runtime_service.get_conversation_tool_ledger(
                                    conversation_id
                                )
                                conversation_turn_store = turn_store
                                conversation_item_stream = await agent.runtime_service.get_conversation_item_stream(
                                    conversation_id
                                )

                                done_payload = {
                                    "message_id": assistant_message.id,
                                    "thought": (reasoning_summary or thought),
                                    "answer": full_content,
                                }
                                if isinstance(rag_metrics, dict):
                                    done_payload["rag_metrics"] = rag_metrics
                                if conversation_context_state:
                                    done_payload["context_state"] = conversation_context_state
                                if conversation_turn_store:
                                    done_payload["turn_store"] = conversation_turn_store
                                if conversation_tool_ledger:
                                    done_payload["tool_ledger"] = conversation_tool_ledger
                                if conversation_item_stream:
                                    done_payload["item_stream"] = conversation_item_stream
                                if isinstance(reasoning_summary, str) and reasoning_summary.strip():
                                    done_payload["reasoning_summary"] = reasoning_summary.strip()

                                yield f"data: {json.dumps({'event': 'done', 'data': done_payload})}\n\n"
                else:
                    yield f"data: {json.dumps({'event': 'phase', 'data': _phase_payload('loading_context', first_turn=is_first_turn)})}\n\n"
                    yield f"data: {json.dumps({'event': 'phase', 'data': _phase_payload('routing', first_turn=is_first_turn)})}\n\n"
                    planner = _create_direct_planner()
                    direct_response = None
                    if prepared_send_plan and str(prepared_send_plan.get("preview_mode") or "") == "direct":
                        logger.info(f"[Chat] 复用完整预演 send_plan（禁用工具）: conv={conversation_id}")
                    else:
                        direct_response = await planner.prepare_direct_response(
                            agent_messages,
                            force_no_tools=True,
                        )
                    if prepared_send_plan and str(prepared_send_plan.get("preview_mode") or "") == "direct":
                        context_debug = None
                        llm_messages = [
                            dict(item)
                            for item in list(prepared_send_plan.get("llm_messages") or [])
                            if isinstance(item, dict)
                        ]
                        system_prompt = str(prepared_send_plan.get("system_prompt") or "")
                    else:
                        if direct_response is None:
                            raise RuntimeError("direct response planner returned no result")
                        context_debug = dict(direct_response.context.context_debug or {})
                        llm_messages = [dict(item) for item in list(direct_response.llm_messages or []) if isinstance(item, dict)]
                        system_prompt = direct_response.system_prompt

                    yield f"data: {json.dumps({'event': 'phase', 'data': _phase_payload('waiting_model', first_turn=is_first_turn)})}\n\n"
                    yield f"data: {json.dumps({'event': 'model_info', 'data': {'provider': getattr(llm_service, 'provider', ''), 'model': (getattr(llm_service, 'config', {}) or {}).get('model')}})}\n\n"
                    if isinstance(context_debug, dict) and context_debug:
                        yield f"data: {json.dumps({'event': 'context_debug', 'data': context_debug})}\n\n"

                    async for chunk in llm_service.chat_stream(
                        messages=llm_messages,
                        system_prompt=system_prompt,
                        temperature=settings.react_temperature,
                        max_tokens=settings.llm_max_tokens,
                    ):
                        full_content += chunk
                        yield f"data: {json.dumps({'event': 'content', 'data': chunk})}\n\n"

                    async with async_session_factory() as save_db:
                        assistant_message = Message(
                            conversation_id=conversation_id,
                            role=MessageRole.ASSISTANT,
                            content=full_content,
                            message_type=MessageType.TEXT,
                        )
                        save_db.add(assistant_message)
                        await save_db.commit()
                        await save_db.refresh(assistant_message)
                        await append_message_item_entry(
                            conversation_id=conversation_id,
                            role="assistant",
                            content=full_content,
                            turn_id=turn_id,
                            message_id=assistant_message.id,
                            created_at=assistant_message.created_at,
                            thought=None,
                            metadata=None,
                            kind="assistant_message",
                        )
                        turn_store = await _finalize_turn(
                            status_value="completed",
                            assistant_message_id=assistant_message.id,
                            assistant_content=full_content,
                        )
                        get_conversation_context_compaction_service().enqueue_conversation(conversation_id)
                        conversation_context_state = await planner.runtime_service.get_conversation_context_state(
                            conversation_id
                        )
                        conversation_tool_ledger = await planner.runtime_service.get_conversation_tool_ledger(
                            conversation_id
                        )
                        conversation_turn_store = turn_store
                        conversation_item_stream = await planner.runtime_service.get_conversation_item_stream(
                            conversation_id
                        )

                        done_payload = {
                            "message_id": assistant_message.id,
                            "answer": full_content,
                        }
                        if conversation_context_state:
                            done_payload["context_state"] = conversation_context_state
                        if conversation_turn_store:
                            done_payload["turn_store"] = conversation_turn_store
                        if conversation_tool_ledger:
                            done_payload["tool_ledger"] = conversation_tool_ledger
                        if conversation_item_stream:
                            done_payload["item_stream"] = conversation_item_stream
                        yield f"data: {json.dumps({'event': 'done', 'data': done_payload})}\n\n"
                
            except asyncio.CancelledError:
                logger.warning(
                    f"[Chat] 流式响应被取消: conv={conversation_id}, turn={turn_id}, "
                    f"content_len={len(full_content)}, iterations={current_iteration}"
                )
                current_task = asyncio.current_task()
                pending_cancel_requests = current_task.cancelling() if current_task is not None else 0
                if current_task is not None and pending_cancel_requests:
                    for _ in range(pending_cancel_requests):
                        current_task.uncancel()
                try:
                    await _finalize_turn(
                        status_value="stopped",
                        assistant_content=full_content,
                        assistant_thought=thought,
                        iteration_count=current_iteration or None,
                        error_message="stream_cancelled",
                    )
                except Exception:
                    logger.exception(
                        "[Chat] 取消后收尾 turn 失败: conv={}, turn={}",
                        conversation_id,
                        turn_id,
                    )
                finally:
                    if current_task is not None and pending_cancel_requests:
                        for _ in range(pending_cancel_requests):
                            current_task.cancel()
                raise
            except Exception as e:
                logger.error(f"流式响应错误: {e}")
                await _finalize_turn(
                    status_value="failed",
                    assistant_content=full_content,
                    assistant_thought=thought,
                    error_message=str(e),
                )
                yield f"data: {json.dumps({'event': 'error', 'data': str(e)})}\n\n"
        
        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )
    else:
        # 非流式响应
        try:
            if use_tools:
                direct_response = None
                if prepared_send_plan and str(prepared_send_plan.get("preview_mode") or "") == "direct":
                    llm_messages = [
                        dict(item)
                        for item in list(prepared_send_plan.get("llm_messages") or [])
                        if isinstance(item, dict)
                    ]
                    system_prompt = str(prepared_send_plan.get("system_prompt") or "")
                else:
                    planner = _create_direct_planner()
                    direct_response = await planner.prepare_direct_response(agent_messages)
                    if direct_response is not None:
                        llm_messages = [dict(item) for item in list(direct_response.llm_messages or []) if isinstance(item, dict)]
                        system_prompt = direct_response.system_prompt
                    else:
                        agent = _create_chat_agent()
                        answer = ""
                        thought = ""
                        reasoning_summary = ""
                        rag_metrics = None
                        async for event in agent.run(agent_messages, stream=False, prepared_plan=prepared_send_plan):
                            event_type = str(event.get("type") or "")
                            event_data = event.get("data")
                            if event_type == "content":
                                answer += str(event_data or "")
                            elif event_type == "answer":
                                answer = str(event_data or "")
                            elif event_type == "thought":
                                thought = str(event_data or "")
                            elif event_type == "done" and isinstance(event_data, dict):
                                if event_data.get("answer"):
                                    answer = str(event_data.get("answer") or "")
                                if event_data.get("thought"):
                                    thought = str(event_data.get("thought") or "")
                                if isinstance(event_data.get("reasoning_summary"), str):
                                    reasoning_summary = str(event_data.get("reasoning_summary") or "")
                                if isinstance(event_data.get("rag_metrics"), dict):
                                    rag_metrics = dict(event_data.get("rag_metrics") or {})
                            elif event_type == "error":
                                raise RuntimeError(str(event_data or "agent run failed"))
                        response = {
                            "content": answer,
                            "thought": reasoning_summary or thought or None,
                            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                            "rag_metrics": rag_metrics,
                        }
                        llm_messages = []
                        system_prompt = ""
                if llm_messages:
                    llm_response = await llm_service.chat(
                        messages=llm_messages,
                        system_prompt=system_prompt,
                        temperature=settings.react_temperature,
                        max_tokens=settings.llm_max_tokens,
                    )
                    response = {
                        "content": llm_response["content"],
                        "thought": None,
                        "usage": llm_response["usage"],
                        "rag_metrics": None,
                    }
            else:
                planner = _create_direct_planner()
                if prepared_send_plan and str(prepared_send_plan.get("preview_mode") or "") == "direct":
                    llm_messages = [
                        dict(item)
                        for item in list(prepared_send_plan.get("llm_messages") or [])
                        if isinstance(item, dict)
                    ]
                    system_prompt = str(prepared_send_plan.get("system_prompt") or "")
                else:
                    direct_response = await planner.prepare_direct_response(agent_messages, force_no_tools=True)
                    if direct_response is None:
                        raise RuntimeError("direct response planner returned no result")
                    llm_messages = [dict(item) for item in list(direct_response.llm_messages or []) if isinstance(item, dict)]
                    system_prompt = direct_response.system_prompt
                llm_response = await llm_service.chat(
                    messages=llm_messages,
                    system_prompt=system_prompt,
                    temperature=settings.react_temperature,
                    max_tokens=settings.llm_max_tokens,
                )
                response = {
                    "content": llm_response["content"],
                    "thought": None,
                    "usage": llm_response["usage"],
                    "rag_metrics": None,
                }
            assistant_message = Message(
                conversation_id=conversation.id,
                role=MessageRole.ASSISTANT,
                content=response["content"],
                message_type=MessageType.TEXT,
                prompt_tokens=response["usage"]["prompt_tokens"],
                completion_tokens=response["usage"]["completion_tokens"],
                total_tokens=response["usage"]["total_tokens"],
                thought=response.get("thought"),
            )
            db.add(assistant_message)
            await db.commit()
            await db.refresh(assistant_message)
            await append_message_item_entry(
                conversation_id=conversation.id,
                role="assistant",
                content=response["content"],
                turn_id=turn_id,
                message_id=assistant_message.id,
                created_at=assistant_message.created_at,
                thought=response.get("thought"),
                kind="assistant_message",
            )
            await runtime_service.append_conversation_item_entries(
                conversation.id,
                [
                    {
                        "kind": "reasoning_summary",
                        "turn_id": turn_id,
                        "role": "assistant",
                        "message_id": assistant_message.id,
                        "iteration": 1,
                        "summary": _assistant_summary_text(response["content"], limit=240),
                        "content": _assistant_summary_text(response["content"], limit=240),
                        "created_at": assistant_message.created_at.isoformat() if assistant_message.created_at else datetime.utcnow().isoformat(),
                    }
                ]
                if _assistant_summary_text(response["content"], limit=240)
                else [],
            )
            await runtime_service.upsert_conversation_turn_entry(
                conversation.id,
                {
                    "turn_id": turn_id,
                    "status": "completed",
                    "assistant_message_id": assistant_message.id,
                    "assistant_summary": _assistant_summary_text(response["content"]),
                    "iteration_count": 1,
                    "completed_at": assistant_message.created_at.isoformat() if assistant_message.created_at else datetime.utcnow().isoformat(),
                },
            )
            get_conversation_context_compaction_service().enqueue_conversation(conversation.id)
            conversation_context_state = await runtime_service.get_conversation_context_state(conversation.id)
            conversation_turn_store = await runtime_service.get_conversation_turn_store(conversation.id)
            conversation_tool_ledger = await runtime_service.get_conversation_tool_ledger(conversation.id)
            conversation_item_stream = await runtime_service.get_conversation_item_stream(conversation.id)
            
            return {
                "conversation_id": conversation.id,
                "message": message_to_response(assistant_message),
                "usage": response["usage"],
                "context_state": conversation_context_state if conversation_context_state else None,
                "turn_store": conversation_turn_store if conversation_turn_store else None,
                "tool_ledger": conversation_tool_ledger if conversation_tool_ledger else None,
                "item_stream": conversation_item_stream if conversation_item_stream else None,
            }
            
        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            await runtime_service.upsert_conversation_turn_entry(
                conversation.id,
                {
                    "turn_id": turn_id,
                    "status": "failed",
                    "error_message": str(e),
                    "completed_at": datetime.utcnow().isoformat(),
                },
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"AI 响应失败: {str(e)}"
            )


@router.get("/conversations/{conversation_id}/messages", response_model=List[MessageResponse])
async def get_messages(
    conversation_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取对话消息列表"""
    # 验证对话所有权
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="对话不存在"
        )
    
    runtime_service = get_agent_runtime_service()
    item_stream = _normalized_item_stream_payload(
        await runtime_service.get_conversation_item_stream(conversation_id)
    )
    if item_stream is None:
        await _raise_if_conversation_has_legacy_messages_without_item_stream(
            conversation_id=conversation_id,
            db=db,
        )
        return []

    return _project_messages_from_item_stream(
        conversation_id=conversation_id,
        item_stream_payload=item_stream,
        skip=skip,
        limit=limit,
    )


@router.post("/messages/stopped", response_model=MessageResponse)
async def save_stopped_message(
    request: SaveStoppedMessageRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """保存被停止的消息"""
    # 验证对话所有权
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == request.conversation_id,
            Conversation.user_id == current_user.id
        )
    )
    conversation = result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="对话不存在"
        )
    
    raw_metadata = dict(request.metadata or {}) if isinstance(request.metadata, dict) else {}
    turn_id = str(raw_metadata.get("turn_id") or "").strip() or None
    persisted_metadata = _sanitized_persisted_chat_metadata(raw_metadata)

    # 创建停止的消息
    message = Message(
        conversation_id=request.conversation_id,
        role=MessageRole.ASSISTANT,
        content=request.content,
        message_type=MessageType.TEXT,
        thought=request.thought,
        metadata_=persisted_metadata,
    )
    
    db.add(message)
    await db.commit()
    await db.refresh(message)
    await append_message_item_entry(
        conversation_id=request.conversation_id,
        role="assistant",
        content=message.content,
        turn_id=turn_id,
        message_id=message.id,
        created_at=message.created_at,
        thought=message.thought,
        metadata=persisted_metadata or None,
        kind="stopped_assistant_message",
    )
    if turn_id:
        runtime_service = get_agent_runtime_service()
        await runtime_service.append_conversation_item_entries(
            request.conversation_id,
            [
                {
                    "kind": "reasoning_summary",
                    "turn_id": turn_id,
                    "role": "assistant",
                    "message_id": message.id,
                    "summary": _assistant_summary_text(message.thought or message.content, limit=240),
                    "content": _assistant_summary_text(message.thought or message.content, limit=240),
                    "created_at": message.created_at.isoformat() if message.created_at else datetime.utcnow().isoformat(),
                }
            ]
            if _assistant_summary_text(message.thought or message.content, limit=240)
            else [],
        )
        await runtime_service.upsert_conversation_turn_entry(
            request.conversation_id,
            {
                "turn_id": turn_id,
                "status": "stopped",
                "assistant_message_id": message.id,
                "assistant_summary": _assistant_summary_text(message.content, fallback=message.thought),
                "completed_at": message.created_at.isoformat() if message.created_at else datetime.utcnow().isoformat(),
            },
        )

    logger.info(f"保存停止消息: conv={request.conversation_id}, msg={message.id}")
    
    return message_to_response(message)
