"""
聊天路由
"""
import asyncio
import hashlib
import json
import re
from datetime import datetime, timedelta
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
from app.models.agent import AgentRun
from app.models.conversation import Conversation, Message, MessageRole, MessageType
from app.models.knowledge import KnowledgeBase
from app.schemas.chat import (
    ConversationCreate, ConversationResponse, ConversationListResponse,
    MessageResponse, ChatRequest, SaveStoppedMessageRequest, ConversationCompactResponse,
    ChatContextPreviewRequest, ChatContextPreviewResponse,
    MessageSpanRewriteRequest, MessageSpanRewriteResponse,
    ChatWorkflowActionResponse, ChatWorkflowControlResponse,
)
from app.services.llm_service import LLMService
from app.services.agent_tools import get_tool_registry
from app.services.agent_runtime_service import get_agent_runtime_service
from app.services.chat_context_store import ConversationItemStreamStore
from app.services.agent_skill_service import get_agent_skill_service
from app.services.conversation_context_compaction_service import (
    ConversationItemStreamUnavailableError,
    get_conversation_context_compaction_service,
)
from app.services.chat_background_run_service import get_chat_background_run_manager

router = APIRouter()

_PERSISTED_CHAT_BACKGROUND_EVENTS = {
    "run_status",
    "start",
    "phase",
    "thinking_start",
    "thought",
    "action",
    "observation",
    "context_debug",
    "done",
    "error",
    "cancelled",
}


def _safe_json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _sse_event(event: str, data: Any) -> str:
    return f"data: {_safe_json_dumps({'event': event, 'data': data})}\n\n"


def _sanitize_reasoning_summary_text(value: object, *, limit: int = 240) -> Optional[str]:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return None
    patterns = (
        r"(让我|我来|我先|我会|我将|我们将|接下来|下一步)",
        r"(准备创建|准备修改|准备写入|将使用|会使用|将创建|将修改|将写入)",
        r"(blocker 已移除|已经解决，因为我们将|fix-[a-z0-9_-]+\.sh)",
    )
    if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns):
        return None
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)].rstrip() + "…"


async def _persist_reasoning_summary_later(
    *,
    conversation_id: int,
    turn_id: str,
    message_id: int,
    run_id: Optional[str],
    iteration_count: Optional[int],
    trace: str,
    created_at: Optional[datetime],
    runtime_service: Any,
    session_factory: Any,
) -> None:
    trace_text = str(trace or "").strip()
    if not trace_text:
        return
    try:
        from app.services.react_agent import AgentCore

        summary_text = await AgentCore.generate_reasoning_summary_from_trace(trace_text)
        compacted = _sanitize_reasoning_summary_text(summary_text or "", limit=240)
        if not compacted:
            return

        async with session_factory() as save_db:
            message = await save_db.get(Message, int(message_id))
            if message and int(message.conversation_id) == int(conversation_id):
                message.thought = compacted
                await save_db.commit()

        await runtime_service.append_conversation_item_entries(
            int(conversation_id),
            [
                {
                    "kind": "reasoning_summary",
                    "turn_id": turn_id,
                    "role": "assistant",
                    "message_id": int(message_id),
                    "run_id": str(run_id or "").strip() or None,
                    "iteration": max(int(iteration_count or 0), 0),
                    "summary": compacted,
                    "content": compacted,
                    "created_at": created_at.isoformat() if created_at else datetime.utcnow().isoformat(),
                }
            ],
        )
        if str(run_id or "").strip():
            await runtime_service.append_chat_run_event(
                str(run_id).strip(),
                event="reasoning_summary",
                data={
                    "summary": compacted,
                    "conversation_id": int(conversation_id),
                    "turn_id": turn_id,
                    "message_id": int(message_id),
                },
            )
        logger.info(
            "[Chat] reasoning summary persisted asynchronously: conv={}, turn={}, msg={}",
            conversation_id,
            turn_id,
            message_id,
        )
    except Exception as exc:
        logger.warning(
            "[Chat] async reasoning summary failed: conv={}, turn={}, msg={}, error={}",
            conversation_id,
            turn_id,
            message_id,
            exc,
        )


def _schedule_reasoning_summary_persist(
    *,
    conversation_id: int,
    turn_id: str,
    message_id: int,
    run_id: Optional[str],
    iteration_count: Optional[int],
    trace: Optional[str],
    created_at: Optional[datetime],
    runtime_service: Any,
    session_factory: Any,
) -> None:
    trace_text = str(trace or "").strip()
    if not trace_text:
        return
    asyncio.create_task(
        _persist_reasoning_summary_later(
            conversation_id=conversation_id,
            turn_id=turn_id,
            message_id=message_id,
            run_id=run_id,
            iteration_count=iteration_count,
            trace=trace_text,
            created_at=created_at,
            runtime_service=runtime_service,
            session_factory=session_factory,
        )
    )


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


_PAPER_STAGE_LABELS = {
    "planning": "规划阶段",
    "implementation_prep": "实施准备阶段",
    "run_drafts": "运行草案阶段",
    "execution": "执行阶段",
    "tuning": "调参与对比阶段",
}

_PAPER_DEFAULT_STAGE_ORDER = (
    "planning",
    "implementation_prep",
    "run_drafts",
    "execution",
    "tuning",
)

_PAPER_TOOL_STAGE_HINTS = {
    "paper_research_status": "planning",
    "paper_research_prepare": "planning",
    "paper_research_clone_repo": "implementation_prep",
    "paper_research_probe_repo": "implementation_prep",
    "paper_research_get_artifact_manifest": "implementation_prep",
    "paper_research_read_artifact": "implementation_prep",
    "paper_research_read_repo_file": "implementation_prep",
    "paper_research_search_repo": "implementation_prep",
    "paper_research_inspect_runtime": "implementation_prep",
    "paper_research_read_implementation_spec": "implementation_prep",
    "paper_research_write_implementation_spec": "implementation_prep",
    "paper_research_read_run_drafts": "run_drafts",
    "paper_research_write_run_drafts": "run_drafts",
    "paper_research_write_execution_spec": "execution",
    "paper_research_read_execution_spec": "execution",
    "paper_research_start_execution": "execution",
    "paper_research_read_execution": "execution",
    "paper_research_cancel_execution": "execution",
}

_PAPER_STAGE_COMPLETION_TOOLS = {
    "planning": {"paper_research_prepare"},
    "implementation_prep": {"paper_research_write_implementation_spec"},
    "run_drafts": {"paper_research_write_run_drafts"},
}


def _build_visible_skill_launch_message(skill_launch: object) -> str:
    stage = str(getattr(skill_launch, "stage", "") or "").strip()
    skill_name = str(getattr(skill_launch, "skill_name", "") or "").strip() or "skill"
    label = _PAPER_STAGE_LABELS.get(stage, stage or "启动")
    paper_id = getattr(skill_launch, "paper_id", None)
    if paper_id:
        return f"继续论文 {label}（paper_id={paper_id}）"
    return f"继续 {skill_name} · {label}"


def _build_visible_chat_message(request: ChatRequest) -> str:
    message = _normalized_optional_text(request.message)
    if message:
        return message
    if request.skill_launch is not None:
        return _build_visible_skill_launch_message(request.skill_launch)
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="消息内容不能为空")


def _build_user_message_metadata(request: ChatRequest) -> Optional[dict]:
    if request.skill_launch is None:
        return None
    payload = request.skill_launch.model_dump(exclude_none=True)
    return {"skill_launch": payload}


def _resolve_effective_agent_message(request: ChatRequest) -> tuple[str, str]:
    visible_message = _build_visible_chat_message(request)
    if request.skill_launch is None:
        return visible_message, visible_message
    service = get_agent_skill_service()
    launch_payload = request.skill_launch.model_dump(exclude_none=True)
    try:
        expanded_message = service.render_launch_prompt(
            str(request.skill_launch.skill_name or "").strip(),
            launch_payload,
        )
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not str(expanded_message or "").strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="skill_launch 未生成有效启动消息")
    return visible_message, str(expanded_message).strip()


def _parse_stage_policy_map(raw_policies: object) -> dict[str, str]:
    policies: dict[str, str] = {}
    for item in raw_policies if isinstance(raw_policies, (list, tuple)) else []:
        text = str(item or "").strip()
        if not text or "=" not in text:
            continue
        stage_name, policy = text.split("=", 1)
        stage_name = str(stage_name or "").strip()
        policy = str(policy or "").strip()
        if stage_name and policy:
            policies[stage_name] = policy
    return policies


def _workflow_stage_index(stage_names: list[str], stage: Optional[str]) -> int:
    normalized_stage = str(stage or "").strip()
    if not normalized_stage:
        return -1
    try:
        return stage_names.index(normalized_stage)
    except ValueError:
        return -1


def _normalize_paper_runtime_stage(stage: object) -> Optional[str]:
    normalized_stage = str(stage or "").strip().lower()
    if not normalized_stage:
        return None
    if normalized_stage in {"env_setup", "data_prep", "implementation_prep"}:
        return "implementation_prep"
    if normalized_stage in {"baseline_repro", "execution"}:
        return "execution"
    if normalized_stage in {"tuning", "compare"}:
        return "tuning"
    if normalized_stage in _PAPER_DEFAULT_STAGE_ORDER:
        return normalized_stage
    return None


def _extract_workflow_status_from_tool_payload(payload: object) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    background_execution = (
        dict(payload.get("background_execution") or {})
        if isinstance(payload.get("background_execution"), dict)
        else {}
    )
    for candidate in (background_execution.get("status"), payload.get("status")):
        normalized = str(candidate or "").strip().lower()
        if normalized:
            return normalized
    return None


def _infer_paper_workflow_stage_from_tool_event(
    *,
    request_stage: str,
    tool_name: object,
    tool_payload: object = None,
    stage_names: Optional[list[str]] = None,
) -> Optional[str]:
    normalized_tool_name = str(tool_name or "").strip()
    normalized_request_stage = str(request_stage or "").strip()
    resolved_stage_names = [str(item).strip() for item in list(stage_names or []) if str(item).strip()]
    if not resolved_stage_names:
        resolved_stage_names = list(_PAPER_DEFAULT_STAGE_ORDER)

    inferred_stage: Optional[str] = None
    if isinstance(tool_payload, dict):
        background_execution = (
            dict(tool_payload.get("background_execution") or {})
            if isinstance(tool_payload.get("background_execution"), dict)
            else {}
        )
        status_summary = (
            dict(tool_payload.get("status_summary") or {})
            if isinstance(tool_payload.get("status_summary"), dict)
            else {}
        )
        inferred_stage = (
            _normalize_paper_runtime_stage(background_execution.get("stage"))
            or _normalize_paper_runtime_stage(status_summary.get("current_stage"))
            or _normalize_paper_runtime_stage(tool_payload.get("current_stage"))
        )
    if inferred_stage is None:
        inferred_stage = _PAPER_TOOL_STAGE_HINTS.get(normalized_tool_name)

    if inferred_stage and _workflow_stage_index(resolved_stage_names, inferred_stage) >= 0:
        if _workflow_stage_index(resolved_stage_names, inferred_stage) >= _workflow_stage_index(
            resolved_stage_names,
            normalized_request_stage,
        ):
            return inferred_stage
    if _workflow_stage_index(resolved_stage_names, normalized_request_stage) >= 0:
        return normalized_request_stage
    return inferred_stage


def _build_workflow_control_payload(
    request: ChatRequest,
    *,
    stage_override: Optional[str] = None,
    stage_status: str = "completed",
) -> Optional[dict]:
    skill_launch = request.skill_launch
    if skill_launch is None:
        return None
    skill_name = str(skill_launch.skill_name or "").strip()
    requested_stage = str(skill_launch.stage or "").strip()
    stage = str(stage_override or requested_stage).strip()
    if not skill_name or not stage:
        return None

    skill = get_agent_skill_service().get_skill(skill_name)
    if skill is None:
        return None

    stage_names = [str(item).strip() for item in skill.stage_names if str(item).strip()] or list(_PAPER_DEFAULT_STAGE_ORDER)
    if _workflow_stage_index(stage_names, stage) < 0:
        stage = requested_stage
    if _workflow_stage_index(stage_names, stage) < 0:
        return None

    stage_policy_map = _parse_stage_policy_map(skill.stage_policies)
    continue_policy = stage_policy_map.get(stage) or str(skill.default_continue_policy or "").strip() or None

    next_stage: Optional[str] = None
    current_index = _workflow_stage_index(stage_names, stage)
    if stage_status == "completed" and current_index >= 0 and current_index + 1 < len(stage_names):
        next_stage = stage_names[current_index + 1]

    action: Optional[ChatWorkflowActionResponse] = None
    suggested_action: Optional[str] = None
    if next_stage:
        next_skill_launch = skill_launch.model_copy(update={"stage": next_stage})
        next_stage_label = _PAPER_STAGE_LABELS.get(next_stage, next_stage)
        action_label = f"继续 {next_stage_label}"
        suggested_action = action_label
        action = ChatWorkflowActionResponse(
            label=action_label,
            message=_build_visible_skill_launch_message(next_skill_launch),
            skill_launch=next_skill_launch,
        )

    workflow_control = ChatWorkflowControlResponse(
        skill_name=skill.name,
        display_name=str(skill.display_name or "").strip() or None,
        stage=stage,
        stage_label=_PAPER_STAGE_LABELS.get(stage, stage),
        stage_status=stage_status,
        continue_policy=continue_policy,
        next_stage=next_stage,
        next_stage_label=_PAPER_STAGE_LABELS.get(next_stage, next_stage) if next_stage else None,
        suggested_action=suggested_action,
        action=action,
    )
    return workflow_control.model_dump(exclude_none=True)


def _build_tool_event_workflow_control_payload(
    request: ChatRequest,
    *,
    tool_name: object,
    tool_payload: object = None,
    success: Optional[bool] = None,
    phase: str = "action",
) -> Optional[dict]:
    skill_launch = request.skill_launch
    if skill_launch is None:
        return None
    skill_name = str(skill_launch.skill_name or "").strip()
    if skill_name != "paper-reproduction":
        return None

    skill = get_agent_skill_service().get_skill(skill_name)
    if skill is None:
        return None

    stage_names = [str(item).strip() for item in skill.stage_names if str(item).strip()] or list(_PAPER_DEFAULT_STAGE_ORDER)
    stage = _infer_paper_workflow_stage_from_tool_event(
        request_stage=str(skill_launch.stage or "").strip(),
        tool_name=tool_name,
        tool_payload=tool_payload,
        stage_names=stage_names,
    )
    if not stage:
        return None

    normalized_tool_name = str(tool_name or "").strip()
    normalized_phase = str(phase or "action").strip().lower()
    tool_status = _extract_workflow_status_from_tool_payload(tool_payload)
    stage_status = "running"

    if normalized_phase == "observation":
        if tool_status in {"failed", "error", "blocked", "cancelled", "canceled"}:
            stage_status = "blocked"
        elif success is False:
            stage_status = "blocked"
        elif (
            stage in _PAPER_STAGE_COMPLETION_TOOLS
            and normalized_tool_name in _PAPER_STAGE_COMPLETION_TOOLS[stage]
            and bool(success)
        ):
            stage_status = "completed"
        elif normalized_tool_name in {"paper_research_start_execution", "paper_research_read_execution", "paper_research_cancel_execution"}:
            if tool_status in {"completed", "complete", "success", "done"}:
                stage_status = "completed"
            else:
                stage_status = "running"

    return _build_workflow_control_payload(
        request,
        stage_override=stage,
        stage_status=stage_status,
    )


def _attach_workflow_control(
    done_payload: dict[str, Any],
    request: ChatRequest,
    *,
    workflow_control: Optional[dict] = None,
) -> dict[str, Any]:
    workflow_control = workflow_control or _build_workflow_control_payload(request)
    if workflow_control:
        done_payload["workflow_control"] = workflow_control
    return done_payload


def _normalized_workflow_control_payload(payload: object) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    try:
        return ChatWorkflowControlResponse.model_validate(payload).model_dump(exclude_none=True)
    except Exception:
        return None


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
    compacted_reasoning = _sanitize_reasoning_summary_text(
        normalized.get("last_reasoning_summary"),
        limit=240,
    )
    if compacted_reasoning:
        normalized["last_reasoning_summary"] = compacted_reasoning
    else:
        normalized.pop("last_reasoning_summary", None)
    normalized["evidence_ledger"] = [
        item
        for item in (_normalized_evidence_entry(raw) for raw in list(normalized.get("evidence_ledger") or []))
        if item is not None
    ]
    workflow_binding = normalized.get("workflow_binding") or {}
    try:
        from app.services.react_agent import ReActAgent

        decision_state = ReActAgent._normalize_decision_state(
            normalized.get("decision_state") or {},
            workflow_binding=workflow_binding,
        )
    except Exception:
        decision_state = {}
    if decision_state:
        normalized["decision_state"] = decision_state
    else:
        normalized.pop("decision_state", None)
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


def _normalized_message_citation_source_item(payload: object, *, label_hint: Optional[str] = None) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None

    label = str(payload.get("label") or label_hint or "").strip()
    if not label:
        return None
    if not (
        (label.startswith("来源") and label[2:].isdigit())
        or (label.startswith("网页") and label[2:].isdigit())
    ):
        return None

    normalized: dict[str, Any] = {"label": label}
    for key in (
        "source_kind",
        "tool_name",
        "title",
        "domain",
        "url",
        "knowledge_base",
        "document",
        "source_label",
        "citation_label",
        "provider",
        "provider_route",
        "content_preview",
    ):
        text = str(payload.get(key) or "").strip()
        if text:
            normalized[key] = text

    for key in ("rank", "chunk_index"):
        try:
            value = payload.get(key)
            if value is not None:
                normalized[key] = int(value)
        except (TypeError, ValueError):
            continue

    try:
        score = payload.get("retrieval_score")
        if score is not None:
            normalized["retrieval_score"] = round(float(score), 1)
    except (TypeError, ValueError):
        pass

    retrieval_scope = payload.get("retrieval_scope")
    if isinstance(retrieval_scope, dict):
        normalized["retrieval_scope"] = _normalized_chat_rag_overrides_payload(
            {"enabled": True, **dict(retrieval_scope)}
        ) or {
            key: value
            for key, value in dict(retrieval_scope).items()
            if key in {"scope_mode", "knowledge_base_ids", "document_ids"}
        }

    return normalized


def _normalized_message_citation_index(payload: object) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    normalized: dict[str, Any] = {}
    for key, value in payload.items():
        item = _normalized_message_citation_source_item(value, label_hint=str(key or "").strip())
        if item is not None:
            normalized[str(item["label"])] = item
    return normalized or None


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


async def _cleanup_stale_conversation_chat_runs(
    *,
    conversation_id: int,
    db: AsyncSession,
) -> List[str]:
    timeout_seconds = max(
        int(getattr(settings, "agent_run_stale_timeout_seconds", 900) or 900),
        60,
    )
    threshold = datetime.utcnow() - timedelta(seconds=timeout_seconds)
    result = await db.execute(
        select(AgentRun).where(
            AgentRun.conversation_id == int(conversation_id),
            AgentRun.status == "running",
            AgentRun.channel.in_(["chat", "chat_background"]),
            AgentRun.started_at <= threshold,
        )
    )
    rows = list(result.scalars().all())
    if not rows:
        return []

    cleaned_ids: List[str] = []
    cleanup_at = datetime.utcnow()
    for record in rows:
        record.status = "error"
        record.finished_at = cleanup_at
        merged = dict(record.metadata_ or {})
        merged.update(
            {
                "error": "stale_run_cleanup",
                "cleanup_reason": "stale_running_run",
                "cleanup_threshold_seconds": timeout_seconds,
                "cleanup_at": cleanup_at.isoformat(),
            }
        )
        record.metadata_ = merged
        cleaned_ids.append(str(record.id))
    await db.commit()
    logger.warning(
        "[Chat] cleaned stale conversation runs: conversation_id={}, count={}, run_ids={}",
        conversation_id,
        len(cleaned_ids),
        cleaned_ids,
    )
    return cleaned_ids


async def _interrupt_orphaned_conversation_background_runs(
    *,
    conversation_id: int,
    user_id: int,
    db: AsyncSession,
) -> List[str]:
    manager = get_chat_background_run_manager()
    result = await db.execute(
        select(AgentRun).where(
            AgentRun.conversation_id == int(conversation_id),
            AgentRun.user_id == int(user_id),
            AgentRun.status == "running",
            AgentRun.channel.in_(["chat", "chat_background"]),
        )
    )
    rows = list(result.scalars().all())
    if not rows:
        return []

    running_background = [
        row for row in rows if str(row.channel or "").strip() == "chat_background"
    ]
    if not running_background:
        return []

    orphaned_background_ids: List[str] = []
    for record in running_background:
        snapshot = await manager.get(str(record.id), user_id=int(user_id))
        if snapshot is None:
            orphaned_background_ids.append(str(record.id))
    if not orphaned_background_ids:
        return []

    cleanup_at = datetime.utcnow()
    cleaned_ids: List[str] = []
    for record in rows:
        record.status = "error"
        record.finished_at = cleanup_at
        merged = dict(record.metadata_ or {})
        merged.update(
            {
                "error": "background_run_interrupted",
                "cleanup_reason": "orphaned_background_run",
                "cleanup_at": cleanup_at.isoformat(),
            }
        )
        record.metadata_ = merged
        cleaned_ids.append(str(record.id))
    await db.commit()
    logger.warning(
        "[Chat] interrupted orphaned conversation runs: conversation_id={}, background_run_ids={}, affected_run_ids={}",
        conversation_id,
        orphaned_background_ids,
        cleaned_ids,
    )
    return cleaned_ids


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
    citation_index = _normalized_message_citation_index(payload.get("citation_index"))
    if citation_index:
        metadata["citation_index"] = citation_index
    workflow_control = _normalized_workflow_control_payload(payload.get("workflow_control"))
    if workflow_control:
        metadata["workflow_control"] = workflow_control
    rewrites = [
        {
            "rewritten_at": str(item.get("rewritten_at") or "").strip(),
            "instruction": str(item.get("instruction") or "").strip(),
            "selected_text": str(item.get("selected_text") or "").strip()[:240],
        }
        for item in list(payload.get("rewrites") or [])
        if isinstance(item, dict) and str(item.get("rewritten_at") or "").strip()
    ]
    if rewrites:
        metadata["rewrites"] = rewrites[-10:]
    return metadata or None


def _sanitized_chat_message_response_metadata(payload: object) -> Optional[dict]:
    """Expose only stable chat UI metadata from persisted messages."""
    return _sanitized_persisted_chat_metadata(payload)


_CHAT_CITATION_LABEL_RE = re.compile(r"\[(网页\d+|来源\d+)\]")
_CHAT_MARKDOWN_LINE_PREFIX_RE = re.compile(r"^(\s{0,3}(?:#{1,6}\s+)?(?:(?:[-*+]\s+)|(?:\d+[.)]\s+)|(?:>\s+))*)")
_CHAT_MARKDOWN_BOLD_LEADING_LABEL_RE = re.compile(r"^(\*\*)([^*\n]{1,120}?)(\*\*)([:：]?)")


def _extract_chat_citation_labels(text: str) -> set[str]:
    return {str(match.group(1) or "").strip() for match in _CHAT_CITATION_LABEL_RE.finditer(str(text or ""))}


def _extract_chat_citation_labels_in_order(text: str) -> List[str]:
    seen: set[str] = set()
    labels: List[str] = []
    for match in _CHAT_CITATION_LABEL_RE.finditer(str(text or "")):
        label = str(match.group(1) or "").strip()
        if not label or label in seen:
            continue
        seen.add(label)
        labels.append(label)
    return labels


def _align_chat_citation_payload(
    *,
    answer_text: str,
    rag_metrics: object,
    citation_index: object,
) -> tuple[Optional[dict], Optional[dict]]:
    used_labels = _extract_chat_citation_labels_in_order(answer_text)
    used_label_set = set(used_labels)

    normalized_index = _normalized_message_citation_index(citation_index)
    if normalized_index:
        normalized_index = {
            label: item
            for label, item in normalized_index.items()
            if label in used_label_set
        } or None

    normalized_metrics: Optional[dict] = None
    if isinstance(rag_metrics, dict):
        normalized_metrics = dict(rag_metrics)
        available_labels = [
            str(item).strip()
            for item in list(normalized_metrics.get("available_source_labels") or normalized_metrics.get("source_labels") or [])
            if str(item).strip()
        ]
        available_label_set = set(available_labels)
        if not available_labels and normalized_index:
            available_labels = list(normalized_index.keys())
            available_label_set = set(available_labels)

        normalized_metrics["available_source_labels"] = available_labels
        normalized_metrics["available_source_labels_count"] = len(available_labels)
        normalized_metrics["source_labels"] = used_labels
        normalized_metrics["source_labels_count"] = len(used_labels)
        normalized_metrics["answer_citation_count"] = len(used_labels)
        normalized_metrics["citation_required"] = bool(available_label_set)
        normalized_metrics["citation_valid"] = (
            bool(used_labels) and used_label_set.issubset(available_label_set)
            if available_label_set
            else True
        )

    return normalized_metrics, normalized_index


def _chat_span_has_markdown_heading(text: str) -> bool:
    return any(
        re.match(r"^\s{0,3}#{1,6}\s+", line)
        for line in str(text or "").strip().splitlines()
    )


def _markdown_line_prefix(text: str) -> str:
    match = _CHAT_MARKDOWN_LINE_PREFIX_RE.match(str(text or ""))
    return str(match.group(1) or "") if match else ""


def _preserve_markdown_leading_bold_label(original_line: str, replacement_line: str, prefix: str) -> str:
    original_body = str(original_line or "")[len(prefix):]
    replacement = str(replacement_line or "")
    original_match = _CHAT_MARKDOWN_BOLD_LEADING_LABEL_RE.match(original_body)
    if not original_match:
        return replacement
    if replacement[len(prefix):].startswith("**"):
        return replacement
    label = str(original_match.group(2) or "").strip()
    if not label:
        return replacement
    replacement_body = replacement[len(prefix):]
    original_suffix = original_body[original_match.end():].strip()
    if not original_suffix and replacement_body:
        return f"{prefix}**{replacement_body}**"
    if not replacement_body.startswith(label):
        return replacement
    return f"{prefix}**{label}**{replacement_body[len(label):]}"


def _preserve_markdown_line_scaffold(original_line: str, replacement_line: str) -> str:
    original = str(original_line or "")
    replacement = str(replacement_line or "")
    prefix = _markdown_line_prefix(original)
    if prefix and not replacement.startswith(prefix):
        replacement_prefix = _markdown_line_prefix(replacement)
        if replacement_prefix:
            replacement = f"{prefix}{replacement[len(replacement_prefix):].lstrip()}"
        else:
            replacement = f"{prefix}{replacement.lstrip()}"
    elif not prefix:
        replacement_prefix = _markdown_line_prefix(replacement)
        if replacement_prefix:
            replacement = replacement[len(replacement_prefix):].lstrip()
    return _preserve_markdown_leading_bold_label(original, replacement, prefix)


def _preserve_span_rewrite_markdown_scaffold(*, selected_text: str, replacement_text: str) -> str:
    original_lines = str(selected_text or "").splitlines()
    replacement_lines = str(replacement_text or "").strip().splitlines()
    if not original_lines or not replacement_lines:
        return str(replacement_text or "").strip()
    if len(original_lines) != len(replacement_lines):
        return _preserve_markdown_line_scaffold(original_lines[0], str(replacement_text or "").strip())
    return "\n".join(
        _preserve_markdown_line_scaffold(original_line, replacement_line)
        for original_line, replacement_line in zip(original_lines, replacement_lines)
    ).strip()


def _resolve_message_span_offsets(
    content: str,
    selected_text: str,
    *,
    occurrence_index: Optional[int] = None,
    before_context: str = "",
    after_context: str = "",
) -> tuple[int, int]:
    body = str(content or "")
    selected = str(selected_text or "")
    if not selected:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "empty_selected_text", "message": "选区不能为空"},
        )

    starts: List[int] = []
    start = body.find(selected)
    while start >= 0:
        starts.append(start)
        start = body.find(selected, start + max(len(selected), 1))

    if not starts:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "selected_span_not_found", "message": "选区已变化或不在原始 Markdown 中，请重新选择。"},
        )

    chosen_start: Optional[int] = None
    if occurrence_index is not None:
        try:
            idx = int(occurrence_index)
        except (TypeError, ValueError):
            idx = -1
        if 0 <= idx < len(starts):
            chosen_start = starts[idx]
        else:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "selected_span_occurrence_mismatch", "message": "选区出现次数已变化，请重新选择。"},
            )

    if chosen_start is None and len(starts) == 1:
        chosen_start = starts[0]

    if chosen_start is None:
        before = str(before_context or "").strip()
        after = str(after_context or "").strip()
        scored: List[tuple[int, int]] = []
        for candidate in starts:
            score = 0
            if before:
                tail = body[max(0, candidate - len(before) - 200) : candidate]
                if tail.endswith(before) or before.endswith(tail[-min(len(tail), len(before)) :]):
                    score += 1
            if after:
                head = body[candidate + len(selected) : candidate + len(selected) + len(after) + 200]
                if head.startswith(after) or after.startswith(head[: min(len(head), len(after))]):
                    score += 1
            if score:
                scored.append((score, candidate))
        scored = sorted(scored, key=lambda item: item[0], reverse=True)
        if len(scored) == 1 or (len(scored) > 1 and scored[0][0] > scored[1][0]):
            chosen_start = scored[0][1]

    if chosen_start is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "selected_span_ambiguous", "message": "这段文本在消息中出现多次，请缩小选区后重试。"},
        )

    return chosen_start, chosen_start + len(selected)


def _build_message_span_rewrite_prompt(
    *,
    instruction: str,
    selected_text: str,
    before_context: str,
    after_context: str,
) -> str:
    return (
        "You are rewriting only one selected span inside an existing assistant answer.\n\n"
        "Task:\n"
        "Rewrite the selected span according to the user's instruction.\n\n"
        "Hard constraints:\n"
        "1. Output only the replacement text for the selected span.\n"
        "2. Do not output the full answer.\n"
        "3. Do not include the surrounding before/after text.\n"
        "4. Do not add new facts, examples, URLs, or citation labels.\n"
        "5. Preserve the factual meaning of the selected span unless the instruction explicitly asks to simplify or remove details.\n"
        "6. Preserve any citation labels already present in the selected span, such as [网页1] or [来源1].\n"
        "7. If the selected span has no citation labels, do not add citation labels.\n"
        "8. Preserve Markdown scaffolding exactly: heading markers, leading numbering, list bullets, blockquote markers, and bold wrappers such as **label**.\n"
        "9. If the selected span has multiple Markdown lines, keep the same line structure and rewrite only the prose inside those lines.\n"
        "10. Do not mention that this is a rewrite. No preface, no explanation.\n\n"
        "Language and style:\n"
        "- Match the language of the selected span unless the instruction explicitly asks otherwise.\n"
        "- Match the surrounding answer's tone and formatting.\n"
        "- Preserve Markdown syntax if the selected span contains Markdown.\n\n"
        f"User rewrite instruction:\n{instruction}\n\n"
        f"Selected span to rewrite:\n<selected_span>\n{selected_text}\n</selected_span>\n\n"
        "Surrounding context for continuity only. Do not rewrite or repeat it:\n"
        f"<before>\n{before_context}\n</before>\n\n"
        f"<after>\n{after_context}\n</after>\n"
    )


def _validate_span_rewrite_replacement(*, selected_text: str, replacement_text: str) -> str:
    replacement = str(replacement_text or "").strip()
    if not replacement:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "empty_rewrite_result", "message": "模型没有返回可用的改写结果。"},
        )
    if len(replacement) > max(len(str(selected_text or "")) * 4, 2000):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "rewrite_result_too_long", "message": "模型返回内容过长，已拒绝替换。"},
        )
    if not _chat_span_has_markdown_heading(selected_text) and _chat_span_has_markdown_heading(replacement):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "rewrite_added_markdown_heading",
                "message": "模型改写新增了 Markdown 标题，已拒绝替换。",
            },
        )
    original_labels = _extract_chat_citation_labels(selected_text)
    replacement_labels = _extract_chat_citation_labels(replacement)
    added_labels = replacement_labels - original_labels
    if added_labels:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "rewrite_added_citation_label",
                "message": "模型改写时新增了不存在的引用标签，已拒绝替换。",
                "added_labels": sorted(added_labels),
            },
        )
    missing_labels = original_labels - replacement_labels
    if missing_labels:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "rewrite_dropped_citation_label",
                "message": "模型改写时删除了原选区引用标签，已拒绝替换。",
                "missing_labels": sorted(missing_labels),
            },
        )
    return replacement


def _iter_sse_payloads_from_buffer(buffer: str, *, flush: bool = False) -> tuple[List[dict], str]:
    if not buffer:
        return [], ""
    lines = buffer.split("\n")
    remainder = "" if flush else (lines.pop() or "")
    payloads: List[dict] = []
    for raw_line in lines:
        line = str(raw_line or "").strip()
        if not line.startswith("data:"):
            continue
        raw_payload = line[5:].strip()
        if not raw_payload:
            continue
        try:
            parsed = json.loads(raw_payload)
        except Exception:
            continue
        if isinstance(parsed, dict):
            payloads.append(parsed)
    if flush and remainder.strip().startswith("data:"):
        try:
            parsed = json.loads(remainder.strip()[5:].strip())
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            payloads.append(parsed)
    return payloads, remainder


def _update_message_content_in_item_stream_payload(
    item_stream_payload: Optional[dict],
    *,
    message_id: int,
    new_content: str,
    rewrite_metadata: Optional[dict] = None,
) -> Optional[dict]:
    if not isinstance(item_stream_payload, dict):
        return None
    payload = dict(item_stream_payload)
    entries = [
        dict(item) if isinstance(item, dict) else item
        for item in list(payload.get("entries") or [])
    ]
    changed = False
    next_entries = []
    for entry in entries:
        if not isinstance(entry, dict):
            next_entries.append(entry)
            continue
        try:
            entry_message_id = int(entry.get("message_id") or 0)
        except Exception:
            entry_message_id = 0
        if entry_message_id == int(message_id) and str(entry.get("role") or "").strip().lower() == "assistant":
            metadata = dict(entry.get("metadata") or {}) if isinstance(entry.get("metadata"), dict) else {}
            if rewrite_metadata:
                rewrites = [dict(item) for item in list(metadata.get("rewrites") or []) if isinstance(item, dict)]
                rewrites.append(dict(rewrite_metadata))
                metadata["rewrites"] = rewrites[-10:]
            next_entries.append({**entry, "content": new_content, "metadata": metadata})
            changed = True
            continue
        next_entries.append(entry)
    if not changed:
        return None
    payload["updated_at"] = datetime.utcnow().isoformat()
    payload["entries"] = next_entries
    payload["version"] = str(payload.get("version") or "conversation_item_stream.v1")
    return payload


def _agent_run_to_chat_run_response(record: AgentRun) -> dict:
    metadata = dict(record.metadata_ or {}) if isinstance(record.metadata_, dict) else {}
    return {
        "run_id": str(record.id),
        "user_id": int(record.user_id),
        "status": str(record.status or ""),
        "conversation_id": int(record.conversation_id) if record.conversation_id is not None else None,
        "channel": str(record.channel or ""),
        "created_at": record.started_at.isoformat() if record.started_at else None,
        "updated_at": (record.finished_at or record.started_at).isoformat() if (record.finished_at or record.started_at) else None,
        "completed_at": record.finished_at.isoformat() if record.finished_at else None,
        "error": str(metadata.get("error") or "").strip() or None,
        "result": dict(metadata.get("background_result") or {}) if isinstance(metadata.get("background_result"), dict) else {},
        "event_count": 0,
    }


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

    stale_run_ids = await _cleanup_stale_conversation_chat_runs(
        conversation_id=conversation.id,
        db=db,
    )
    orphaned_run_ids = await _interrupt_orphaned_conversation_background_runs(
        conversation_id=conversation.id,
        user_id=current_user.id,
        db=db,
    )
    if stale_run_ids or orphaned_run_ids:
        await get_agent_runtime_service().cleanup_stale_conversation_turns(
            conversation_id=conversation.id,
            older_than_seconds=60,
        )
        await db.refresh(conversation)
    
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

    conversation_state = await runtime_service.get_conversation_context_state(conversation_id) if conversation_id else {}
    active_skill_names = [
        str(item or "").strip()
        for item in list((conversation_state or {}).get("active_skill_names") or [])
        if str(item or "").strip()
    ]

    tool_registry = get_tool_registry(
        db=None,
        user_id=current_user.id,
        db_session_factory=async_session_factory,
        conversation_id=conversation_id,
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
            active_skill_names=active_skill_names,
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
        prefetched_rag_messages=getattr(prepared.context, "prefetched_rag_messages", None),
        prefetched_rag_metadata=getattr(prepared.context, "prefetched_rag_metadata", None),
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


@router.post("/runs")
async def create_chat_background_run(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
):
    """Start a chat run that continues in the backend after the SSE client disconnects."""
    from app.core.database import async_session_factory

    runtime_service = get_agent_runtime_service()
    llm_provider = request.llm_provider or current_user.preferred_llm_provider
    run_id = await runtime_service.create_run(
        user_id=current_user.id,
        channel="chat_background",
        conversation_id=request.conversation_id,
        intent="chat",
        selected_tools=[],
        model_provider=llm_provider,
        model_name=(settings.get_llm_config(llm_provider) or {}).get("model"),
        metadata={
            "path": "chat_background_run",
            "request_conversation_id": request.conversation_id,
            "stream_source": "/api/v1/chat/send",
        },
    )
    manager = get_chat_background_run_manager()
    user_snapshot = SimpleNamespace(
        id=current_user.id,
        preferred_llm_provider=current_user.preferred_llm_provider,
    )
    background_request = request.model_copy(update={"stream": True})

    async def _finalize_background_turn(
        *,
        status_value: str,
        error_message: Optional[str] = None,
    ) -> None:
        conversation_id = start_payload.get("conversation_id") or request.conversation_id
        turn_id = str(start_payload.get("turn_id") or "").strip()
        if not conversation_id or not turn_id:
            return
        try:
            await runtime_service.upsert_conversation_turn_entry(
                int(conversation_id),
                {
                    "turn_id": turn_id,
                    "status": status_value,
                    "run_id": str(run_id),
                    "error_message": error_message,
                    "completed_at": datetime.utcnow().isoformat(),
                },
            )
        except Exception:
            logger.exception(
                "[ChatBackgroundRun] finalize turn failed: run_id={}, conversation_id={}, turn_id={}",
                run_id,
                conversation_id,
                turn_id,
            )

    async def _persist_event(payload: dict) -> None:
        event = str(payload.get("event") or "").strip()
        if event not in _PERSISTED_CHAT_BACKGROUND_EVENTS:
            return
        await runtime_service.append_chat_run_event(
            run_id,
            event=event,
            data=payload.get("data"),
            created_at=str(payload.get("created_at") or ""),
        )

    async def _execute(publish):
        await publish("run_status", {"run_id": run_id, "status": "running"})
        buffer = ""
        start_payload: dict[str, Any] = {}
        done_payload: dict[str, Any] = {}
        try:
            async with async_session_factory() as run_db:
                response = await send_message(background_request, current_user=user_snapshot, db=run_db)
                if not isinstance(response, StreamingResponse):
                    done_payload = response if isinstance(response, dict) else {"response": str(response)}
                    await publish("done", done_payload)
                else:
                    async for raw_chunk in response.body_iterator:
                        chunk = raw_chunk.decode("utf-8") if isinstance(raw_chunk, (bytes, bytearray)) else str(raw_chunk)
                        buffer += chunk
                        payloads, buffer = _iter_sse_payloads_from_buffer(buffer)
                        for payload in payloads:
                            event = str(payload.get("event") or "").strip()
                            data = payload.get("data")
                            if event == "start" and isinstance(data, dict):
                                start_payload = dict(data)
                            if event == "done" and isinstance(data, dict):
                                done_payload = dict(data)
                            if event:
                                await publish(event, data)
                    payloads, _ = _iter_sse_payloads_from_buffer(buffer, flush=True)
                    for payload in payloads:
                        event = str(payload.get("event") or "").strip()
                        data = payload.get("data")
                        if event == "start" and isinstance(data, dict):
                            start_payload = dict(data)
                        if event == "done" and isinstance(data, dict):
                            done_payload = dict(data)
                        if event:
                            await publish(event, data)
            await runtime_service.complete_run(
                run_id,
                status="completed",
                metadata={
                    "background_result": {
                        "start": start_payload,
                        "done": done_payload,
                    },
                    "conversation_id": start_payload.get("conversation_id") or request.conversation_id,
                    "turn_id": start_payload.get("turn_id"),
                    "agent_run_id": done_payload.get("run_id"),
                },
            )
            return {"start": start_payload, "done": done_payload}
        except asyncio.CancelledError:
            await _finalize_background_turn(
                status_value="stopped",
                error_message="background_run_cancelled",
            )
            await runtime_service.complete_run(
                run_id,
                status="cancelled",
                metadata={"error": "cancelled", "background_result": {"start": start_payload}},
            )
            raise
        except Exception as exc:
            await _finalize_background_turn(
                status_value="failed",
                error_message=str(exc),
            )
            await runtime_service.complete_run(
                run_id,
                status="error",
                metadata={"error": str(exc), "background_result": {"start": start_payload}},
            )
            raise

    return await manager.start(
        run_id=run_id,
        user_id=current_user.id,
        execute_fn=_execute,
        persist_event_fn=_persist_event,
    )


@router.get("/runs/{run_id}")
async def get_chat_background_run(
    run_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    manager = get_chat_background_run_manager()
    snapshot = await manager.get(run_id, user_id=current_user.id)
    if snapshot is not None:
        return snapshot
    record = await db.get(AgentRun, str(run_id or "").strip())
    if not record or int(record.user_id) != int(current_user.id) or not str(record.channel or "").startswith("chat"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="后台对话任务不存在")
    return _agent_run_to_chat_run_response(record)


@router.get("/conversations/{conversation_id}/active-run")
async def get_active_conversation_chat_run(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the latest running background chat run for a conversation, if any."""
    conversation_result = await db.execute(
        select(Conversation).where(
            Conversation.id == int(conversation_id),
            Conversation.user_id == int(current_user.id),
        )
    )
    conversation = conversation_result.scalar_one_or_none()
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="对话不存在")

    stale_run_ids = await _cleanup_stale_conversation_chat_runs(
        conversation_id=int(conversation_id),
        db=db,
    )
    orphaned_run_ids = await _interrupt_orphaned_conversation_background_runs(
        conversation_id=int(conversation_id),
        user_id=current_user.id,
        db=db,
    )
    if stale_run_ids or orphaned_run_ids:
        await get_agent_runtime_service().cleanup_stale_conversation_turns(
            conversation_id=int(conversation_id),
            older_than_seconds=60,
        )

    run_result = await db.execute(
        select(AgentRun)
        .where(
            AgentRun.user_id == int(current_user.id),
            AgentRun.conversation_id == int(conversation_id),
            AgentRun.channel == "chat_background",
            AgentRun.status == "running",
        )
        .order_by(desc(AgentRun.started_at), desc(AgentRun.id))
        .limit(1)
    )
    record = run_result.scalar_one_or_none()
    if record is None:
        return None

    manager = get_chat_background_run_manager()
    snapshot = await manager.get(str(record.id), user_id=current_user.id)
    if snapshot is not None:
        return snapshot
    return _agent_run_to_chat_run_response(record)


@router.post("/runs/{run_id}/cancel")
async def cancel_chat_background_run(
    run_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    runtime_service = get_agent_runtime_service()
    manager = get_chat_background_run_manager()
    snapshot = await manager.cancel(run_id, user_id=current_user.id)
    if snapshot is not None:
        await runtime_service.complete_run(
            str(run_id),
            status="cancelled",
            metadata={"error": "cancel_requested"},
        )
        return snapshot
    record = await db.get(AgentRun, str(run_id or "").strip())
    if not record or int(record.user_id) != int(current_user.id) or not str(record.channel or "").startswith("chat"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="后台对话任务不存在")
    await runtime_service.complete_run(
        str(run_id),
        status="cancelled",
        metadata={"error": "cancel_requested"},
    )
    await db.refresh(record)
    return _agent_run_to_chat_run_response(record)


@router.get("/runs/{run_id}/stream")
async def stream_chat_background_run(
    run_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    manager = get_chat_background_run_manager()
    runtime_service = get_agent_runtime_service()
    snapshot = await manager.get(run_id, user_id=current_user.id)
    has_memory_record = snapshot is not None
    persisted_events: List[dict] = []
    if snapshot is None:
        record = await db.get(AgentRun, str(run_id or "").strip())
        if not record or int(record.user_id) != int(current_user.id) or not str(record.channel or "").startswith("chat"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="后台对话任务不存在")
        if str(record.status or "").strip().lower() == "running":
            await runtime_service.complete_run(
                str(run_id),
                status="error",
                metadata={"error": "background_run_interrupted"},
            )
            await db.refresh(record)
        snapshot = _agent_run_to_chat_run_response(record)
        persisted_events = await runtime_service.list_chat_run_events(str(run_id), limit=1000)

    async def _generate():
        yield _sse_event("run_status", snapshot)
        if not has_memory_record:
            for payload in persisted_events:
                event = str(payload.get("event") or "").strip()
                if not event or event == "run_status":
                    continue
                yield _sse_event(event, payload.get("data"))
            if snapshot.get("status") == "error" and snapshot.get("error") == "background_run_interrupted":
                yield _sse_event("error", "后台对话任务已中断，可能是服务重启或进程切换导致。")
            return
        async for payload in manager.subscribe(run_id, user_id=current_user.id, replay=True):
            event = str(payload.get("event") or "").strip()
            if not event:
                continue
            yield _sse_event(event, payload.get("data"))

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/send")
async def send_message(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """发送消息（支持流式响应和工具调用）"""
    visible_message, effective_agent_message = _resolve_effective_agent_message(request)

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
            title=visible_message[:50] + "..." if len(visible_message) > 50 else visible_message,
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
        draft_message=visible_message,
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
        content=visible_message,
        message_type=MessageType.TEXT,
        metadata_=_build_user_message_metadata(request),
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
    await runtime_service.cleanup_stale_conversation_turns(
        conversation_id=conversation.id,
        older_than_seconds=300,
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
    
    agent_messages = [{"role": "user", "content": effective_agent_message}]
    
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

    conversation_state = await runtime_service.get_conversation_context_state(conversation_id) if conversation_id else {}
    persisted_active_skill_names = [
        str(item or "").strip()
        for item in list((conversation_state or {}).get("active_skill_names") or [])
        if str(item or "").strip()
    ]

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
            active_skill_names=(
                [str(request.skill_launch.skill_name).strip()]
                if request.skill_launch is not None and str(request.skill_launch.skill_name or "").strip()
                else list(persisted_active_skill_names)
            ),
            chat_preferences_override=runtime_service.normalize_chat_preference_overrides(effective_chat_preferences),
            rag_overrides=runtime_service.normalize_chat_rag_overrides(effective_rag_overrides),
        )

    def _create_chat_agent():
        tool_registry = get_tool_registry(
            db=None,
            user_id=current_user.id,
            db_session_factory=async_session_factory,
            conversation_id=conversation_id,
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
            conversation_id=conversation_id,
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
            citation_index = None
            context_debug = None
            reasoning_summary = None
            reasoning_trace = None
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
                compacted = _sanitize_reasoning_summary_text(summary_text or "", limit=240)
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
                yield _sse_event("start", {"conversation_id": conversation_id, "message_id": user_message.id, "turn_id": turn_id})
                
                if use_tools:
                    # 先走轻量 planner 判断是否可以直答；只有真的需要工具时才初始化完整 agent。
                    planner = None
                    direct_response = None
                    if prepared_send_plan and str(prepared_send_plan.get("preview_mode") or "") == "direct":
                        logger.info(f"[Chat] 复用完整预演 send_plan: conv={conversation_id}")
                    else:
                        yield _sse_event("phase", _phase_payload("loading_context", first_turn=is_first_turn))
                        yield _sse_event("phase", _phase_payload("routing", first_turn=is_first_turn))
                        planner = _create_direct_planner()
                        direct_response = await planner.prepare_direct_response(agent_messages)
                    if prepared_send_plan and str(prepared_send_plan.get("preview_mode") or "") == "direct":
                        logger.info(
                            f"[Chat] 复用直连流式回答 send_plan: conv={conversation_id}, "
                            f"intent={((prepared_send_plan.get('routing_decision') or {}).get('intent') or 'unknown')}"
                        )
                        yield _sse_event("phase", _phase_payload("waiting_model", first_turn=is_first_turn))
                        yield _sse_event("model_info", {"provider": getattr(llm_service, "provider", ""), "model": (getattr(llm_service, "config", {}) or {}).get("model")})
                        context_debug = None

                        async for chunk in llm_service.chat_stream(
                            messages=[dict(item) for item in list(prepared_send_plan.get("llm_messages") or []) if isinstance(item, dict)],
                            system_prompt=str(prepared_send_plan.get("system_prompt") or ""),
                            temperature=settings.react_temperature,
                            max_tokens=settings.llm_max_tokens,
                        ):
                            full_content += chunk
                            yield _sse_event("content", chunk)

                        logger.info(f"[Chat] 复用直连 send_plan 完成: content_len={len(full_content)}")
                        async with async_session_factory() as save_db:
                            message_metadata = _sanitized_persisted_chat_metadata(
                                {"workflow_control": _build_workflow_control_payload(request)}
                            )
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

                            done_payload = _attach_workflow_control(done_payload, request)
                            yield _sse_event("done", done_payload)
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
                        yield _sse_event("phase", _phase_payload("waiting_model", first_turn=is_first_turn))
                        yield _sse_event("model_info", {"provider": getattr(llm_service, "provider", ""), "model": (getattr(llm_service, "config", {}) or {}).get("model")})
                        if isinstance(direct_response.context.context_debug, dict) and direct_response.context.context_debug:
                            context_debug = direct_response.context.context_debug
                            yield _sse_event("context_debug", context_debug)

                        async for chunk in llm_service.chat_stream(
                            messages=direct_response.llm_messages,
                            system_prompt=direct_response.system_prompt,
                            temperature=settings.react_temperature,
                            max_tokens=settings.llm_max_tokens,
                        ):
                            full_content += chunk
                            yield _sse_event("content", chunk)

                        logger.info(f"[Chat] 直连流式完成: content_len={len(full_content)}")
                        async with async_session_factory() as save_db:
                            message_metadata = _sanitized_persisted_chat_metadata(
                                {"workflow_control": _build_workflow_control_payload(request)}
                            )
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

                            done_payload = _attach_workflow_control(done_payload, request)
                            yield _sse_event("done", done_payload)
                        return

                    # 使用 ReAct Agent（带工具）
                    yield _sse_event("phase", _phase_payload("waiting_model", first_turn=is_first_turn))
                    agent = _create_chat_agent()
                    active_workflow_control: Optional[dict] = None
                    
                    async for event in agent.run(agent_messages, stream=True, prepared_plan=prepared_send_plan):
                        event_type = event["type"]
                        event_data = event["data"]
                        
                        if event_type == "start":
                            yield _sse_event("model_info", event_data)
                        elif event_type == "thinking_start":
                            current_iteration += 1
                            yield _sse_event("thinking_start", {"iteration": current_iteration})
                        elif event_type == "thinking":
                            yield _sse_event("thinking", event_data)
                        elif event_type == "thought":
                            thought = event_data
                            raw_thought = str(event_data or "").strip()
                            compacted_thought = " ".join(raw_thought.split()).strip()
                            if len(compacted_thought) > 400:
                                compacted_thought = compacted_thought[:399].rstrip() + "…"
                            if raw_thought:
                                await runtime_service.append_conversation_item_entries(
                                    conversation_id,
                                    [
                                        {
                                            "kind": "thought",
                                            "turn_id": turn_id,
                                            "role": "assistant",
                                            "run_id": str(getattr(agent.runtime_context, "run_id", "") or "").strip() or None,
                                            "iteration": max(int(current_iteration or 0), 0),
                                            "summary": compacted_thought or raw_thought,
                                            "content": raw_thought,
                                            "created_at": datetime.utcnow().isoformat(),
                                        }
                                    ],
                                )
                            yield _sse_event("thought", event_data)
                        elif event_type == "action":
                            action_payload = dict(event_data or {}) if isinstance(event_data, dict) else {"value": event_data}
                            workflow_control = _build_tool_event_workflow_control_payload(
                                request,
                                tool_name=action_payload.get("tool"),
                                tool_payload=action_payload.get("data"),
                                phase="action",
                            )
                            if workflow_control:
                                action_payload["workflow_control"] = workflow_control
                                active_workflow_control = workflow_control
                            yield _sse_event("action", action_payload)
                        elif event_type == "observation":
                            observation_payload = dict(event_data or {}) if isinstance(event_data, dict) else {"value": event_data}
                            workflow_control = _build_tool_event_workflow_control_payload(
                                request,
                                tool_name=observation_payload.get("tool"),
                                tool_payload=observation_payload.get("data"),
                                success=(
                                    observation_payload.get("success")
                                    if "success" in observation_payload
                                    else None
                                ),
                                phase="observation",
                            )
                            if workflow_control:
                                observation_payload["workflow_control"] = workflow_control
                                active_workflow_control = workflow_control
                            yield _sse_event("observation", observation_payload)
                        elif event_type == "context_debug":
                            if isinstance(event_data, dict):
                                context_debug = event_data
                            yield _sse_event("context_debug", event_data)
                        elif event_type == "content":
                            full_content += event_data
                            yield _sse_event("content", event_data)
                        elif event_type == "answer":
                            full_content = event_data
                            yield _sse_event("content", event_data)
                        elif event_type == "error":
                            logger.error(f"[Chat] ReAct Agent 错误: {event_data}")
                            yield _sse_event("error", event_data)
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
                                if isinstance(event_data.get("_reasoning_trace"), str):
                                    reasoning_trace = event_data["_reasoning_trace"]
                                if isinstance(event_data.get("citation_index"), dict):
                                    citation_index = dict(event_data.get("citation_index") or {})

                            rag_metrics, citation_index = _align_chat_citation_payload(
                                answer_text=full_content,
                                rag_metrics=rag_metrics,
                                citation_index=citation_index,
                            )

                            logger.info(f"[Chat] 对话完成: iterations={current_iteration}, content_len={len(full_content)}")
                            persisted_thought = _sanitize_reasoning_summary_text(
                                reasoning_summary or thought or "",
                                limit=240,
                            )
                            
                            # 保存助手消息（包含完整的ReAct步骤）
                            async with async_session_factory() as save_db:
                                message_metadata: dict[str, Any] = {}
                                if isinstance(rag_metrics, dict):
                                    message_metadata["rag_metrics"] = rag_metrics
                                if isinstance(citation_index, dict):
                                    message_metadata["citation_index"] = citation_index
                                workflow_control = active_workflow_control or _build_workflow_control_payload(request)
                                if workflow_control:
                                    message_metadata["workflow_control"] = workflow_control
                                persisted_message_metadata = _sanitized_persisted_chat_metadata(message_metadata) or {}
                                assistant_message = Message(
                                    conversation_id=conversation_id,
                                    role=MessageRole.ASSISTANT,
                                    content=full_content,
                                    message_type=MessageType.TEXT,
                                    thought=persisted_thought,
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
                                    thought=persisted_thought,
                                    metadata=persisted_message_metadata or None,
                                    kind="assistant_message",
                                )
                                await _append_reasoning_item(
                                    summary_text=reasoning_summary,
                                    message_id=assistant_message.id,
                                    run_id=str(event_data.get("run_id") or "").strip() or None,
                                    iteration_count=current_iteration,
                                    created_at=assistant_message.created_at,
                                )
                                _schedule_reasoning_summary_persist(
                                    conversation_id=conversation_id,
                                    turn_id=turn_id,
                                    message_id=assistant_message.id,
                                    run_id=str(event_data.get("run_id") or "").strip() or None,
                                    iteration_count=current_iteration,
                                    trace=reasoning_trace,
                                    created_at=assistant_message.created_at,
                                    runtime_service=agent.runtime_service,
                                    session_factory=async_session_factory,
                                )
                                turn_store = await _finalize_turn(
                                    status_value="completed",
                                    assistant_message_id=assistant_message.id,
                                    assistant_content=full_content,
                                    assistant_thought=persisted_thought,
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
                                    "thought": persisted_thought,
                                    "answer": full_content,
                                }
                                if isinstance(rag_metrics, dict):
                                    done_payload["rag_metrics"] = rag_metrics
                                if isinstance(citation_index, dict):
                                    done_payload["citation_index"] = citation_index
                                if conversation_context_state:
                                    done_payload["context_state"] = conversation_context_state
                                if conversation_turn_store:
                                    done_payload["turn_store"] = conversation_turn_store
                                if conversation_tool_ledger:
                                    done_payload["tool_ledger"] = conversation_tool_ledger
                                if conversation_item_stream:
                                    done_payload["item_stream"] = conversation_item_stream
                                if persisted_thought:
                                    done_payload["reasoning_summary"] = persisted_thought

                                done_payload = _attach_workflow_control(
                                    done_payload,
                                    request,
                                    workflow_control=active_workflow_control,
                                )
                                yield _sse_event("done", done_payload)
                else:
                    yield _sse_event("phase", _phase_payload("loading_context", first_turn=is_first_turn))
                    yield _sse_event("phase", _phase_payload("routing", first_turn=is_first_turn))
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

                    yield _sse_event("phase", _phase_payload("waiting_model", first_turn=is_first_turn))
                    yield _sse_event("model_info", {"provider": getattr(llm_service, "provider", ""), "model": (getattr(llm_service, "config", {}) or {}).get("model")})
                    if isinstance(context_debug, dict) and context_debug:
                        yield _sse_event("context_debug", context_debug)

                    async for chunk in llm_service.chat_stream(
                        messages=llm_messages,
                        system_prompt=system_prompt,
                        temperature=settings.react_temperature,
                        max_tokens=settings.llm_max_tokens,
                    ):
                        full_content += chunk
                        yield _sse_event("content", chunk)

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
                        done_payload = _attach_workflow_control(done_payload, request)
                        yield _sse_event("done", done_payload)
                
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
                yield _sse_event("error", str(e))
        
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
            reasoning_trace = ""
            run_id = None
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
                        citation_index = None
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
                                if event_data.get("run_id"):
                                    run_id = str(event_data.get("run_id") or "")
                                if isinstance(event_data.get("reasoning_summary"), str):
                                    reasoning_summary = str(event_data.get("reasoning_summary") or "")
                                if isinstance(event_data.get("_reasoning_trace"), str):
                                    reasoning_trace = str(event_data.get("_reasoning_trace") or "")
                                if isinstance(event_data.get("rag_metrics"), dict):
                                    rag_metrics = dict(event_data.get("rag_metrics") or {})
                                if isinstance(event_data.get("citation_index"), dict):
                                    citation_index = dict(event_data.get("citation_index") or {})
                            elif event_type == "error":
                                raise RuntimeError(str(event_data or "agent run failed"))
                        response = {
                            "content": answer,
                            "thought": _sanitize_reasoning_summary_text(
                                reasoning_summary or thought or "",
                                limit=240,
                            ),
                            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                            "rag_metrics": rag_metrics,
                            "citation_index": citation_index,
                        }
                        aligned_rag_metrics, aligned_citation_index = _align_chat_citation_payload(
                            answer_text=response["content"],
                            rag_metrics=response.get("rag_metrics"),
                            citation_index=response.get("citation_index"),
                        )
                        response["rag_metrics"] = aligned_rag_metrics
                        response["citation_index"] = aligned_citation_index
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
                    "citation_index": None,
                }
            persisted_message_metadata = _sanitized_persisted_chat_metadata(
                {
                    "rag_metrics": response.get("rag_metrics"),
                    "citation_index": response.get("citation_index"),
                    "workflow_control": _build_workflow_control_payload(request),
                }
            ) or {}
            assistant_message = Message(
                conversation_id=conversation.id,
                role=MessageRole.ASSISTANT,
                content=response["content"],
                message_type=MessageType.TEXT,
                prompt_tokens=response["usage"]["prompt_tokens"],
                completion_tokens=response["usage"]["completion_tokens"],
                total_tokens=response["usage"]["total_tokens"],
                thought=response.get("thought"),
                metadata_=persisted_message_metadata or None,
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
                metadata=persisted_message_metadata or None,
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
            if reasoning_trace:
                _schedule_reasoning_summary_persist(
                    conversation_id=conversation.id,
                    turn_id=turn_id,
                    message_id=assistant_message.id,
                    run_id=run_id,
                    iteration_count=1,
                    trace=reasoning_trace,
                    created_at=assistant_message.created_at,
                    runtime_service=runtime_service,
                    session_factory=async_session_factory,
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
                "workflow_control": _build_workflow_control_payload(request),
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


@router.post("/messages/{message_id}/rewrite-span", response_model=MessageSpanRewriteResponse)
async def rewrite_message_span(
    message_id: int,
    request: MessageSpanRewriteRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Rewrite only a selected span inside one assistant message."""
    result = await db.execute(
        select(Message, Conversation)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Message.id == int(message_id),
            Conversation.user_id == current_user.id,
        )
    )
    row = result.first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="消息不存在",
        )
    message, conversation = row
    role_value = message.role.value if hasattr(message.role, "value") else str(message.role)
    if role_value != MessageRole.ASSISTANT.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "rewrite_only_assistant_message", "message": "只能改写 AI 回复。"},
        )

    old_content = str(message.content or "")
    start_offset, end_offset = _resolve_message_span_offsets(
        old_content,
        request.selected_text,
        occurrence_index=request.occurrence_index,
        before_context=request.before_context,
        after_context=request.after_context,
    )
    selected_text = old_content[start_offset:end_offset]
    selected_labels = _extract_chat_citation_labels(selected_text)

    prompt = _build_message_span_rewrite_prompt(
        instruction=request.instruction,
        selected_text=selected_text,
        before_context=request.before_context,
        after_context=request.after_context,
    )
    try:
        llm = LLMService(conversation.llm_provider or current_user.preferred_llm_provider)
        response = await llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system_prompt="You are a precise text rewriting engine. Return only the requested replacement span.",
            temperature=0.2,
            max_tokens=min(max(len(selected_text) * 2, 256), 2048),
            source="chat.message_span_rewrite",
        )
    except Exception as exc:
        logger.warning(f"[ChatRewrite] LLM rewrite failed message_id={message_id}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "rewrite_llm_failed", "message": "改写模型调用失败，请稍后重试。"},
        )

    replacement_text = _preserve_span_rewrite_markdown_scaffold(
        selected_text=selected_text,
        replacement_text=str(response.get("content") or ""),
    )
    replacement_text = _validate_span_rewrite_replacement(
        selected_text=selected_text,
        replacement_text=replacement_text,
    )
    new_content = old_content[:start_offset] + replacement_text + old_content[end_offset:]

    if new_content == old_content:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "rewrite_noop", "message": "改写结果没有变化。"},
        )

    rewrite_metadata = {
        "rewritten_at": datetime.utcnow().isoformat(),
        "instruction": str(request.instruction or "").strip(),
        "selected_text": selected_text,
        "replacement_text": replacement_text,
        "start_offset": start_offset,
        "end_offset": end_offset,
        "citation_labels": sorted(selected_labels),
        "model": str(response.get("model") or "").strip() or None,
    }

    metadata = dict(message.metadata_ or {}) if isinstance(message.metadata_, dict) else {}
    rewrites = [dict(item) for item in list(metadata.get("rewrites") or []) if isinstance(item, dict)]
    rewrites.append(dict(rewrite_metadata))
    metadata["rewrites"] = rewrites[-10:]
    message.content = new_content
    message.metadata_ = metadata

    conversation_metadata = dict(conversation.metadata_ or {}) if isinstance(conversation.metadata_, dict) else {}
    item_stream = _update_message_content_in_item_stream_payload(
        conversation_metadata.get("item_stream"),
        message_id=message.id,
        new_content=new_content,
        rewrite_metadata=rewrite_metadata,
    )
    if isinstance(conversation_metadata.get("item_stream"), dict) and item_stream is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "message_item_stream_entry_not_found", "message": "没有找到可同步的消息条目，请刷新后重试。"},
        )
    if item_stream is not None:
        conversation_metadata["item_stream"] = item_stream

    turn_store = conversation_metadata.get("turn_store")
    if isinstance(turn_store, dict):
        turn_entries = [
            dict(item) if isinstance(item, dict) else item
            for item in list(turn_store.get("entries") or [])
        ]
        for item in turn_entries:
            if not isinstance(item, dict):
                continue
            try:
                assistant_message_id = int(item.get("assistant_message_id"))
            except Exception:
                assistant_message_id = 0
            if assistant_message_id == int(message.id):
                item["assistant_summary"] = _assistant_summary_text(new_content)
        conversation_metadata["turn_store"] = {
            **dict(turn_store),
            "updated_at": datetime.utcnow().isoformat(),
            "entries": turn_entries,
        }

    conversation.metadata_ = conversation_metadata
    conversation.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(message)

    return MessageSpanRewriteResponse(
        message=message_to_response(message),
        old_content=old_content,
        new_content=new_content,
        selected_text=selected_text,
        replacement_text=replacement_text,
        start_offset=start_offset,
        end_offset=end_offset,
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
