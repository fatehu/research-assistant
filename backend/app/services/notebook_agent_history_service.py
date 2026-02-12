from datetime import datetime
from typing import Any, Dict

from loguru import logger

from app.core.database import async_session_factory
from app.services.notebook_service import NotebookService


HISTORY_ROOT_KEY = "agent_histories"
DEFAULT_MAX_MESSAGES = 100
DEFAULT_RETAIN_MESSAGES = 50


def _utc_now_iso() -> str:
    return datetime.utcnow().isoformat()


def build_empty_history(notebook_id: str) -> Dict[str, Any]:
    now = _utc_now_iso()
    return {
        "notebook_id": notebook_id,
        "messages": [],
        "created_at": now,
        "updated_at": now,
    }


def normalize_history(payload: Any, notebook_id: str) -> Dict[str, Any]:
    history = build_empty_history(notebook_id)
    if not isinstance(payload, dict):
        return history

    if isinstance(payload.get("messages"), list):
        history["messages"] = payload["messages"]
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
    normalized["updated_at"] = _utc_now_iso()
    await persist_history(notebook_id, user_id, channel, normalized)
    return normalized


async def clear_history(notebook_id: str, user_id: int, channel: str) -> Dict[str, Any]:
    empty_history = build_empty_history(notebook_id)
    await persist_history(notebook_id, user_id, channel, empty_history)
    return empty_history
