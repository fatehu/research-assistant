import re
from datetime import datetime
from typing import Any, Dict, List

from loguru import logger

from app.core.database import async_session_factory
from app.services.notebook_service import NotebookService


HISTORY_ROOT_KEY = "agent_histories"
DEFAULT_MAX_MESSAGES = 100
DEFAULT_RETAIN_MESSAGES = 50
DEFAULT_RECENT_MESSAGES = 4


def _truncate_text(value: Any, limit: int = 220) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    compact = re.sub(r"\s+", " ", text)
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)] + "..."


def _normalize_message_list(messages: Any) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    if not isinstance(messages, list):
        return normalized
    for item in messages:
        if isinstance(item, dict):
            normalized.append(dict(item))
    return normalized


def build_history_summary(messages: Any, recent_limit: int = DEFAULT_RECENT_MESSAGES) -> str:
    normalized: List[Dict[str, str]] = []
    for item in _normalize_message_list(messages):
        role = str(item.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = _truncate_text(item.get("content"), limit=220)
        if not content:
            continue
        normalized.append({"role": role, "content": content})

    older = normalized[:-recent_limit] if len(normalized) > recent_limit else []
    summary_lines: List[str] = []
    if older:
        user_goals = [item["content"] for item in older if item["role"] == "user"][-2:]
        assistant_updates = [item["content"] for item in older if item["role"] == "assistant"][-2:]
        issue_candidates = [
            item["content"]
            for item in older
            if any(
                token in item["content"].lower()
                for token in ("报错", "错误", "失败", "timeout", "error", "warning", "超时")
            )
        ][-2:]
        if user_goals:
            summary_lines.append("更早用户目标: " + " | ".join(user_goals))
        if assistant_updates:
            summary_lines.append("更早助手结论: " + " | ".join(assistant_updates))
        if issue_candidates:
            summary_lines.append("更早卡点/异常: " + " | ".join(issue_candidates))
    return "\n".join(summary_lines).strip()


def build_history_summary_cache(messages: Any, recent_limit: int = DEFAULT_RECENT_MESSAGES) -> Dict[str, Any]:
    normalized_messages = _normalize_message_list(messages)
    return {
        "recent_limit": int(recent_limit),
        "message_count": len(normalized_messages),
        "summary": build_history_summary(normalized_messages, recent_limit=recent_limit),
        "updated_at": _utc_now_iso(),
    }


def get_cached_history_summary(history: Any, recent_limit: int = DEFAULT_RECENT_MESSAGES) -> str:
    if not isinstance(history, dict):
        return ""

    cache = history.get("summary_cache")
    if not isinstance(cache, dict):
        return ""

    messages = _normalize_message_list(history.get("messages"))
    try:
        cache_recent_limit = int(cache.get("recent_limit", 0) or 0)
        cache_message_count = int(cache.get("message_count", -1) or -1)
    except (TypeError, ValueError):
        return ""

    if cache_recent_limit != int(recent_limit):
        return ""
    if cache_message_count != len(messages):
        return ""

    return str(cache.get("summary") or "").strip()


def _utc_now_iso() -> str:
    return datetime.utcnow().isoformat()


def build_empty_history(notebook_id: str) -> Dict[str, Any]:
    now = _utc_now_iso()
    return {
        "notebook_id": notebook_id,
        "messages": [],
        "summary_cache": build_history_summary_cache([], recent_limit=DEFAULT_RECENT_MESSAGES),
        "created_at": now,
        "updated_at": now,
    }


def normalize_history(payload: Any, notebook_id: str) -> Dict[str, Any]:
    history = build_empty_history(notebook_id)
    if not isinstance(payload, dict):
        return history

    history["messages"] = _normalize_message_list(payload.get("messages"))
    if isinstance(payload.get("summary_cache"), dict):
        history["summary_cache"] = dict(payload["summary_cache"])
    elif history["messages"]:
        history["summary_cache"] = build_history_summary_cache(
            history["messages"],
            recent_limit=DEFAULT_RECENT_MESSAGES,
        )
    if isinstance(payload.get("created_at"), str) and payload["created_at"]:
        history["created_at"] = payload["created_at"]
    if isinstance(payload.get("updated_at"), str) and payload["updated_at"]:
        history["updated_at"] = payload["updated_at"]
    return history


async def load_history(notebook_id: str, user_id: int, channel: str) -> Dict[str, Any]:
    empty_history = build_empty_history(notebook_id)
    try:
        async with async_session_factory() as db:
            service = NotebookService(db)
            notebook_model = await service.get_notebook_model(notebook_id, user_id)
            if not notebook_model:
                return empty_history

            metadata = dict(notebook_model.notebook_metadata or {})
            history_root = metadata.get(HISTORY_ROOT_KEY)
            if not isinstance(history_root, dict):
                return empty_history
            return normalize_history(history_root.get(channel), notebook_id)
    except Exception as exc:
        logger.warning(f"[AgentHistory] Load failed: notebook={notebook_id}, channel={channel}, error={exc}")
        return empty_history


async def persist_history(
    notebook_id: str,
    user_id: int,
    channel: str,
    history: Dict[str, Any],
) -> bool:
    normalized = normalize_history(history, notebook_id)
    try:
        async with async_session_factory() as db:
            service = NotebookService(db)
            notebook_model = await service.get_notebook_model(notebook_id, user_id)
            if not notebook_model:
                return False

            metadata = dict(notebook_model.notebook_metadata or {})
            history_root_raw = metadata.get(HISTORY_ROOT_KEY, {})
            history_root = dict(history_root_raw) if isinstance(history_root_raw, dict) else {}
            history_root[channel] = normalized
            metadata[HISTORY_ROOT_KEY] = history_root

            notebook_model.notebook_metadata = metadata
            notebook_model.updated_at = datetime.utcnow()
            await db.commit()
            return True
    except Exception as exc:
        logger.warning(f"[AgentHistory] Persist failed: notebook={notebook_id}, channel={channel}, error={exc}")
        return False


async def append_history_message(
    notebook_id: str,
    user_id: int,
    channel: str,
    history: Dict[str, Any],
    message: Dict[str, Any],
    max_messages: int = DEFAULT_MAX_MESSAGES,
    retain_messages: int = DEFAULT_RETAIN_MESSAGES,
) -> Dict[str, Any]:
    normalized = normalize_history(history, notebook_id)
    normalized["messages"].append(message)
    if len(normalized["messages"]) > max_messages:
        normalized["messages"] = normalized["messages"][-retain_messages:]
    normalized["summary_cache"] = build_history_summary_cache(
        normalized["messages"],
        recent_limit=DEFAULT_RECENT_MESSAGES,
    )
    normalized["updated_at"] = _utc_now_iso()
    await persist_history(notebook_id, user_id, channel, normalized)
    return normalized


async def clear_history(notebook_id: str, user_id: int, channel: str) -> Dict[str, Any]:
    empty_history = build_empty_history(notebook_id)
    await persist_history(notebook_id, user_id, channel, empty_history)
    return empty_history
