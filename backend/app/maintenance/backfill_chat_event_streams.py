from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.database import async_session_factory
from app.models.conversation import Conversation, Message
from app.services.agent_runtime_service import get_agent_runtime_service
from app.services.chat_context_store import (
    ConversationItemStreamStore,
    ConversationTurnEntry,
    ConversationTurnStore,
    HistoryLog,
    ToolLedgerStore,
)
from app.services.react_agent import AgentCore


def _compact_text(value: Any, limit: int = 220) -> str:
    return AgentCore._compact_debug_text(value, limit)


def _parse_created_at(value: Optional[str]) -> Tuple[int, str]:
    text = str(value or "").strip()
    if not text:
        return (1, "")
    return (0, text)


def _assistant_summary(content: str, thought: Optional[str] = None) -> Optional[str]:
    preferred = _compact_text(thought or "", 240)
    if preferred:
        return preferred
    fallback = _compact_text(content or "", 240)
    return fallback or None


def _message_role_value(message: Message) -> str:
    role = getattr(message, "role", None)
    return role.value if hasattr(role, "value") else str(role or "").strip().lower()


def _message_rows(messages: Iterable[Message]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    current_turn_id: Optional[str] = None
    for message in sorted(list(messages or []), key=lambda item: (getattr(item, "created_at", None) or datetime.utcnow(), int(getattr(item, "id", 0) or 0))):
        role = _message_role_value(message)
        metadata = dict(getattr(message, "metadata_", None) or {}) if isinstance(getattr(message, "metadata_", None), dict) else {}
        if role == "user":
            current_turn_id = str(metadata.get("turn_id") or "").strip() or f"legacy-turn:{int(message.id)}"
        turn_id = str(metadata.get("turn_id") or "").strip() or current_turn_id or f"legacy-turn:{int(message.id)}"
        current_turn_id = turn_id
        kind = "system_message" if role == "system" else f"{role}_message"
        rows.append(
            {
                "kind": kind,
                "turn_id": turn_id,
                "role": role or "assistant",
                "content": str(getattr(message, "content", "") or ""),
                "message_id": int(getattr(message, "id", 0) or 0),
                "thought": str(getattr(message, "thought", "") or "").strip() or None,
                "metadata": metadata or None,
                "created_at": getattr(message, "created_at", None).isoformat() if getattr(message, "created_at", None) else "",
            }
        )
    return rows


def _build_item_entries(
    *,
    messages: Iterable[Message],
    tool_ledger: ToolLedgerStore,
    history_log: HistoryLog,
    compacted_history: Dict[str, Any],
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []

    items.extend(_message_rows(messages))

    for entry in list(tool_ledger.entries or []):
        items.append(
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
                "created_at": entry.created_at or "",
            }
        )

    for event in list(history_log.events or []):
        items.append(
            {
                "kind": "history_event",
                "role": "system",
                "summary": str(event.title or "").strip() or "event",
                "content": str(event.detail or "").strip() or "updated",
                "created_at": event.created_at or "",
            }
        )

    items.sort(key=lambda item: _parse_created_at(item.get("created_at")))

    boundary_message_id = compacted_history.get("compact_boundary_message_id")
    replacement_history = list(compacted_history.get("replacement_history") or [])
    boundary_content = str(compacted_history.get("history_summary") or "").strip()
    boundary_summary = str(compacted_history.get("history_anchors") or "").strip() or None
    boundary_created_at = str(compacted_history.get("updated_at") or "").strip() or datetime.utcnow().isoformat()

    if boundary_message_id or replacement_history or boundary_content or boundary_summary:
        boundary_item = {
            "kind": "compact_boundary",
            "role": "system",
            "content": boundary_content,
            "summary": boundary_summary,
            "status": "backfill",
            "message_id": int(boundary_message_id) if boundary_message_id is not None else None,
            "metadata": {
                "compact_boundary_message_id": int(boundary_message_id) if boundary_message_id is not None else None,
                "replacement_history": replacement_history,
                "compacted_message_count": int(compacted_history.get("compacted_message_count") or 0),
            },
            "created_at": boundary_created_at,
        }

        insert_at = len(items)
        if boundary_message_id is not None:
            last_message_index = None
            for index, item in enumerate(items):
                raw_message_id = item.get("message_id")
                try:
                    message_id = int(raw_message_id) if raw_message_id is not None else None
                except Exception:
                    message_id = None
                if message_id is not None and message_id <= int(boundary_message_id):
                    last_message_index = index
            if last_message_index is not None:
                insert_at = last_message_index + 1
        items.insert(insert_at, boundary_item)

    return items


def _build_turn_store(
    *,
    messages: Iterable[Message],
    tool_ledger: ToolLedgerStore,
) -> Dict[str, Any]:
    grouped_messages: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    grouped_tools: Dict[str, List[Any]] = defaultdict(list)

    for entry in _message_rows(messages):
        turn_id = str(entry.get("turn_id") or "").strip()
        if turn_id:
            grouped_messages[turn_id].append(entry)

    for entry in list(tool_ledger.entries or []):
        turn_id = str(entry.turn_id or "").strip()
        if turn_id:
            grouped_tools[turn_id].append(entry)

    turn_ids = list(dict.fromkeys([*grouped_messages.keys(), *grouped_tools.keys()]))
    turn_store = ConversationTurnStore()
    for turn_id in turn_ids:
        messages = sorted(
            grouped_messages.get(turn_id, []),
            key=lambda item: _parse_created_at(item.get("created_at")),
        )
        tools = sorted(
            grouped_tools.get(turn_id, []),
            key=lambda item: _parse_created_at(getattr(item, "created_at", None)),
        )
        user_entry = next((item for item in messages if str(item.get("role") or "").strip().lower() == "user"), None)
        assistant_entries = [item for item in messages if str(item.get("role") or "").strip().lower() == "assistant"]
        assistant_entry = assistant_entries[-1] if assistant_entries else None
        status = "completed" if assistant_entry else "running"
        if any(str(item.error or "").strip() for item in tools):
            status = "error" if not assistant_entry else status
        iteration_count = max(
            [int(getattr(item, "iteration", 0) or 0) for item in tools] or [0]
        )
        tool_call_count = sum(1 for item in tools if str(item.kind or "").strip() == "tool_call")
        tool_result_count = sum(1 for item in tools if str(item.kind or "").strip() == "tool_result")
        completed_at = None
        if assistant_entry and assistant_entry.get("created_at"):
            completed_at = assistant_entry["created_at"]
        elif tools and getattr(tools[-1], "created_at", None):
            completed_at = tools[-1].created_at

        turn_store.upsert(
            ConversationTurnEntry(
                turn_id=turn_id,
                status=status,
                user_message_id=int(user_entry["message_id"]) if user_entry and user_entry.get("message_id") is not None else None,
                assistant_message_id=int(assistant_entry["message_id"]) if assistant_entry and assistant_entry.get("message_id") is not None else None,
                run_id=None,
                user_content=str(user_entry.get("content") or "").strip() or None if user_entry else None,
                assistant_summary=_assistant_summary(
                    str(assistant_entry.get("content") or ""),
                    assistant_entry.get("thought"),
                ) if assistant_entry else None,
                iteration_count=max(iteration_count, 0),
                tool_call_count=tool_call_count,
                tool_result_count=tool_result_count,
                error_message=next(
                    (
                        str(item.error or "").strip()
                        for item in reversed(tools)
                        if str(item.error or "").strip()
                    ),
                    None,
                ),
                started_at=(user_entry.get("created_at") if user_entry else None)
                or (messages[0].get("created_at") if messages else None)
                or (tools[0].created_at if tools else None),
                completed_at=completed_at,
            )
        )
    turn_store.compact()
    return turn_store.to_payload()


async def _backfill_conversation(
    conversation: Conversation,
    *,
    rebuild: bool,
    dry_run: bool,
) -> Tuple[bool, bool]:
    metadata = dict(conversation.metadata_ or {})
    tool_ledger = ToolLedgerStore.from_payload(metadata.get("tool_ledger"))
    history_log = HistoryLog.from_payload(metadata.get("history_log"))
    compacted_history = dict(metadata.get("compacted_history") or {}) if isinstance(metadata.get("compacted_history"), dict) else {}
    existing_item_stream = ConversationItemStreamStore.from_payload(metadata.get("item_stream"))
    existing_turn_store = ConversationTurnStore.from_payload(metadata.get("turn_store"))

    needs_item_stream = rebuild or not list(existing_item_stream.entries or [])
    needs_turn_store = rebuild or not list(existing_turn_store.entries or [])
    if not needs_item_stream and not needs_turn_store:
        return False, False

    runtime_service = get_agent_runtime_service()
    wrote_item_stream = False
    wrote_turn_store = False

    if needs_item_stream:
        item_entries = _build_item_entries(
            messages=list(conversation.messages or []),
            tool_ledger=tool_ledger,
            history_log=history_log,
            compacted_history=compacted_history,
        )
        if item_entries and not dry_run:
            item_stream = ConversationItemStreamStore()
            item_stream.extend(
                ConversationItemStreamStore.from_payload(
                    {"version": "conversation_item_stream.v1", "entries": item_entries}
                ).entries
            )
            item_stream.compact()
            await runtime_service.upsert_conversation_item_stream(conversation.id, item_stream.to_payload())
        wrote_item_stream = bool(item_entries)

    if needs_turn_store:
        turn_payload = _build_turn_store(
            messages=list(conversation.messages or []),
            tool_ledger=tool_ledger,
        )
        if list((turn_payload.get("entries") or [])) and not dry_run:
            await runtime_service.upsert_conversation_turn_store(conversation.id, turn_payload)
        wrote_turn_store = bool(turn_payload.get("entries"))

    return wrote_item_stream, wrote_turn_store


async def _run(limit: int, rebuild: bool, dry_run: bool) -> None:
    processed = 0
    updated_items = 0
    updated_turns = 0
    async with async_session_factory() as db:
        result = await db.execute(
            select(Conversation)
            .options(selectinload(Conversation.messages))
            .order_by(Conversation.id.asc())
        )
        conversations = list(result.scalars().all())

    for conversation in conversations:
        processed += 1
        wrote_items, wrote_turns = await _backfill_conversation(
            conversation,
            rebuild=rebuild,
            dry_run=dry_run,
        )
        updated_items += int(wrote_items)
        updated_turns += int(wrote_turns)
        if limit > 0 and processed >= limit:
            break

    print(
        f"[chat_event_backfill] processed={processed} "
        f"item_stream_updated={updated_items} turn_store_updated={updated_turns} "
        f"dry_run={str(dry_run).lower()} rebuild={str(rebuild).lower()}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill chat item_stream/turn_store from existing chat artifacts.")
    parser.add_argument("--limit", type=int, default=0, help="Max conversations to process; 0 means all.")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild even if item_stream/turn_store already exist.")
    parser.add_argument("--dry-run", action="store_true", help="Compute results without writing back.")
    args = parser.parse_args()
    asyncio.run(_run(limit=max(int(args.limit or 0), 0), rebuild=bool(args.rebuild), dry_run=bool(args.dry_run)))


if __name__ == "__main__":
    main()
