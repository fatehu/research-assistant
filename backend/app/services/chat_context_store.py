from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
import uuid


def _normalized_optional_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.lower() in {"none", "null"}:
        return None
    return text


@dataclass(frozen=True)
class HistoryEvent:
    title: str
    detail: str
    created_at: str


@dataclass
class HistoryLog:
    events: List[HistoryEvent] = field(default_factory=list)
    updated_at: Optional[str] = None

    def add(self, title: str, detail: str) -> None:
        now = datetime.utcnow().isoformat()
        self.events.append(HistoryEvent(title=title, detail=detail, created_at=now))
        self.updated_at = now

    def compact(self, keep_last: int = 40) -> None:
        if len(self.events) > keep_last:
            self.events[:] = self.events[-keep_last:]
            self.updated_at = datetime.utcnow().isoformat()

    def to_payload(self) -> Dict[str, Any]:
        return {
            "version": "conversation_history_log.v1",
            "updated_at": self.updated_at or datetime.utcnow().isoformat(),
            "events": [asdict(event) for event in self.events],
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any] | None) -> "HistoryLog":
        events: List[HistoryEvent] = []
        updated_at: Optional[str] = None
        if isinstance(payload, dict):
            updated_at = str(payload.get("updated_at") or "").strip() or None
            for item in list(payload.get("events") or []):
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                detail = str(item.get("detail") or "").strip()
                created_at = str(item.get("created_at") or "").strip()
                if title and detail:
                    events.append(HistoryEvent(title=title, detail=detail, created_at=created_at or datetime.utcnow().isoformat()))
        return cls(events=events, updated_at=updated_at)


@dataclass(frozen=True)
class ConversationTurnEntry:
    turn_id: str
    status: str
    user_message_id: Optional[int] = None
    assistant_message_id: Optional[int] = None
    run_id: Optional[str] = None
    user_content: Optional[str] = None
    assistant_summary: Optional[str] = None
    iteration_count: int = 0
    tool_call_count: int = 0
    tool_result_count: int = 0
    error_message: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


@dataclass
class ConversationTurnStore:
    entries: List[ConversationTurnEntry] = field(default_factory=list)
    updated_at: Optional[str] = None

    def upsert(self, entry: ConversationTurnEntry) -> None:
        replaced = False
        for index, current in enumerate(self.entries):
            if current.turn_id == entry.turn_id:
                self.entries[index] = entry
                replaced = True
                break
        if not replaced:
            self.entries.append(entry)
        self.updated_at = datetime.utcnow().isoformat()

    def compact(self, keep_last: int = 80) -> None:
        if len(self.entries) > keep_last:
            self.entries[:] = self.entries[-keep_last:]
            self.updated_at = datetime.utcnow().isoformat()

    def replay(self) -> List[Dict[str, Any]]:
        return [asdict(entry) for entry in self.entries]

    def to_payload(self) -> Dict[str, Any]:
        return {
            "version": "conversation_turn_store.v1",
            "updated_at": self.updated_at or datetime.utcnow().isoformat(),
            "entries": self.replay(),
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any] | None) -> "ConversationTurnStore":
        entries: List[ConversationTurnEntry] = []
        updated_at: Optional[str] = None
        if isinstance(payload, dict):
            updated_at = str(payload.get("updated_at") or "").strip() or None
            for item in list(payload.get("entries") or []):
                if not isinstance(item, dict):
                    continue
                turn_id = str(item.get("turn_id") or "").strip()
                if not turn_id:
                    continue
                status = str(item.get("status") or "").strip().lower() or "running"
                try:
                    iteration_count = max(int(item.get("iteration_count") or 0), 0)
                except Exception:
                    iteration_count = 0
                try:
                    tool_call_count = max(int(item.get("tool_call_count") or 0), 0)
                except Exception:
                    tool_call_count = 0
                try:
                    tool_result_count = max(int(item.get("tool_result_count") or 0), 0)
                except Exception:
                    tool_result_count = 0
                try:
                    user_message_id = int(item["user_message_id"]) if item.get("user_message_id") is not None else None
                except Exception:
                    user_message_id = None
                try:
                    assistant_message_id = int(item["assistant_message_id"]) if item.get("assistant_message_id") is not None else None
                except Exception:
                    assistant_message_id = None
                entries.append(
                    ConversationTurnEntry(
                        turn_id=turn_id,
                        status=status,
                        user_message_id=user_message_id,
                        assistant_message_id=assistant_message_id,
                        run_id=_normalized_optional_text(item.get("run_id")),
                        user_content=_normalized_optional_text(item.get("user_content")),
                        assistant_summary=_normalized_optional_text(item.get("assistant_summary")),
                        iteration_count=iteration_count,
                        tool_call_count=tool_call_count,
                        tool_result_count=tool_result_count,
                        error_message=_normalized_optional_text(item.get("error_message")),
                        started_at=_normalized_optional_text(item.get("started_at")),
                        completed_at=_normalized_optional_text(item.get("completed_at")),
                    )
                )
        return cls(entries=entries, updated_at=updated_at)


@dataclass(frozen=True)
class ToolLedgerEntry:
    entry_id: str
    kind: str
    tool_name: str
    turn_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    run_id: Optional[str] = None
    iteration: int = 0
    status: Optional[str] = None
    arguments: Optional[Dict[str, Any]] = None
    summary: Optional[str] = None
    success: Optional[bool] = None
    error: Optional[str] = None
    permission_required: bool = False
    execution_time_ms: Optional[float] = None
    output_tokens_estimate: Optional[int] = None
    truncated: Optional[bool] = None
    parallel_group: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    created_at: Optional[str] = None


@dataclass
class ToolLedgerStore:
    entries: List[ToolLedgerEntry] = field(default_factory=list)
    updated_at: Optional[str] = None

    def append(self, entry: ToolLedgerEntry) -> None:
        self.entries.append(entry)
        self.updated_at = datetime.utcnow().isoformat()

    def extend(self, entries: List[ToolLedgerEntry]) -> None:
        for entry in entries:
            self.entries.append(entry)
        if entries:
            self.updated_at = datetime.utcnow().isoformat()

    def compact(self, keep_last: int = 120) -> None:
        if len(self.entries) > keep_last:
            self.entries[:] = self.entries[-keep_last:]
            self.updated_at = datetime.utcnow().isoformat()

    def replay(self) -> List[Dict[str, Any]]:
        return [asdict(entry) for entry in self.entries]

    def to_payload(self) -> Dict[str, Any]:
        return {
            "version": "conversation_tool_ledger.v1",
            "updated_at": self.updated_at or datetime.utcnow().isoformat(),
            "entries": self.replay(),
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any] | None) -> "ToolLedgerStore":
        entries: List[ToolLedgerEntry] = []
        updated_at: Optional[str] = None
        if isinstance(payload, dict):
            updated_at = str(payload.get("updated_at") or "").strip() or None
            for item in list(payload.get("entries") or []):
                if not isinstance(item, dict):
                    continue
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
                entries.append(
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
                        created_at=str(item.get("created_at") or "").strip() or None,
                    )
                )
        return cls(entries=entries, updated_at=updated_at)


@dataclass(frozen=True)
class ConversationItemEntry:
    item_id: str
    kind: str
    turn_id: Optional[str] = None
    role: Optional[str] = None
    content: Optional[str] = None
    message_id: Optional[int] = None
    run_id: Optional[str] = None
    iteration: int = 0
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    status: Optional[str] = None
    arguments: Optional[Dict[str, Any]] = None
    thought: Optional[str] = None
    summary: Optional[str] = None
    success: Optional[bool] = None
    error: Optional[str] = None
    permission_required: bool = False
    execution_time_ms: Optional[float] = None
    output_tokens_estimate: Optional[int] = None
    truncated: Optional[bool] = None
    parallel_group: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    created_at: Optional[str] = None


@dataclass(frozen=True)
class ConversationCanonicalHistory:
    active_entries: List[ConversationItemEntry] = field(default_factory=list)
    replacement_history: List[Dict[str, str]] = field(default_factory=list)
    boundary_message_id: Optional[int] = None
    keep_turn_id: Optional[str] = None
    replacement_checkpoint_item_id: Optional[str] = None


@dataclass
class ConversationItemStreamStore:
    entries: List[ConversationItemEntry] = field(default_factory=list)
    updated_at: Optional[str] = None

    def append(self, entry: ConversationItemEntry) -> None:
        self.entries.append(entry)
        self.updated_at = datetime.utcnow().isoformat()

    def extend(self, entries: List[ConversationItemEntry]) -> None:
        for entry in entries:
            self.entries.append(entry)
        if entries:
            self.updated_at = datetime.utcnow().isoformat()

    def compact(self, keep_last: int = 240) -> None:
        if len(self.entries) <= keep_last:
            return

        rows = list(self.entries or [])
        boundary_index: Optional[int] = None
        keep_turn_id: Optional[str] = None

        for index, entry in enumerate(rows):
            if str(entry.kind or "").strip().lower() != "compact_boundary":
                continue
            boundary_index = index
            metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
            keep_turn_id = str(metadata.get("keep_turn_id") or "").strip() or None

        if boundary_index is not None:
            retained: List[ConversationItemEntry] = []
            seen_item_ids: set[str] = set()

            if keep_turn_id:
                for entry in rows[:boundary_index]:
                    if str(entry.turn_id or "").strip() != keep_turn_id:
                        continue
                    if not self._is_message_like(entry):
                        continue
                    if entry.item_id in seen_item_ids:
                        continue
                    retained.append(entry)
                    seen_item_ids.add(entry.item_id)

            for entry in rows[boundary_index:]:
                if entry.item_id in seen_item_ids:
                    continue
                retained.append(entry)
                seen_item_ids.add(entry.item_id)

            if len(retained) < len(rows):
                self.entries[:] = retained
                self.updated_at = datetime.utcnow().isoformat()
            return

        self.entries[:] = rows[-keep_last:]
        self.updated_at = datetime.utcnow().isoformat()

    @staticmethod
    def _is_message_like(entry: ConversationItemEntry) -> bool:
        kind = str(entry.kind or "").strip().lower()
        role = str(entry.role or "").strip().lower()
        return kind in {"message", "user_message", "assistant_message", "system_message"} or role in {
            "user",
            "assistant",
            "system",
        }

    def canonical_history(
        self,
        *,
        fallback_boundary_message_id: Optional[int] = None,
    ) -> ConversationCanonicalHistory:
        rows = list(self.entries or [])
        if not rows:
            return ConversationCanonicalHistory(boundary_message_id=fallback_boundary_message_id)

        boundary_index: Optional[int] = None
        boundary_message_id: Optional[int] = fallback_boundary_message_id
        replacement_history: List[Dict[str, str]] = []
        keep_turn_id: Optional[str] = None
        replacement_checkpoint_item_id: Optional[str] = None

        compact_boundaries: List[tuple[int, ConversationItemEntry, Dict[str, Any], List[Dict[str, str]]]] = []
        for index, item in enumerate(rows):
            if str(item.kind or "").strip().lower() != "compact_boundary":
                continue
            metadata = item.metadata if isinstance(item.metadata, dict) else {}
            normalized_replacement_history = [
                {
                    "role": str(history_item.get("role") or "system").strip().lower() or "system",
                    "content": str(history_item.get("content") or ""),
                }
                for history_item in list(metadata.get("replacement_history") or [])
                if isinstance(history_item, dict) and str(history_item.get("content") or "").strip()
            ]
            compact_boundaries.append((index, item, metadata, normalized_replacement_history))

        if compact_boundaries:
            boundary_index, boundary_item, boundary_metadata, _boundary_replacement_history = compact_boundaries[-1]
            raw_boundary_id = boundary_metadata.get("compact_boundary_message_id")
            try:
                boundary_message_id = int(raw_boundary_id) if raw_boundary_id is not None else boundary_message_id
            except Exception:
                boundary_message_id = boundary_message_id
            keep_turn_id = str(boundary_metadata.get("keep_turn_id") or "").strip() or None

            for _index, item, _metadata, candidate_replacement_history in reversed(compact_boundaries):
                if candidate_replacement_history:
                    replacement_history = list(candidate_replacement_history)
                    replacement_checkpoint_item_id = str(item.item_id or "").strip() or None
                    break

        active_entries = rows[boundary_index + 1 :] if boundary_index is not None else rows
        if boundary_index is not None and keep_turn_id:
            preserved_entries = [
                item
                for item in rows[:boundary_index]
                if str(item.turn_id or "").strip() == keep_turn_id and self._is_message_like(item)
            ]
            active_entries = preserved_entries + active_entries

        if boundary_message_id is not None:
            filtered_entries: List[ConversationItemEntry] = []
            for item in active_entries:
                if not self._is_message_like(item):
                    filtered_entries.append(item)
                    continue
                keep_current_turn = bool(keep_turn_id and str(item.turn_id or "").strip() == keep_turn_id)
                try:
                    message_id = int(item.message_id) if item.message_id is not None else None
                except Exception:
                    message_id = None
                if not keep_current_turn and message_id is not None and message_id <= int(boundary_message_id):
                    continue
                filtered_entries.append(item)
            active_entries = filtered_entries

        return ConversationCanonicalHistory(
            active_entries=list(active_entries),
            replacement_history=replacement_history,
            boundary_message_id=boundary_message_id,
            keep_turn_id=keep_turn_id,
            replacement_checkpoint_item_id=replacement_checkpoint_item_id,
        )

    def canonical_replay_rows(
        self,
        *,
        fallback_boundary_message_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        canonical = self.canonical_history(
            fallback_boundary_message_id=fallback_boundary_message_id,
        )
        rows: List[Dict[str, Any]] = list(canonical.replacement_history)
        for item in canonical.active_entries:
            kind = str(item.kind or "").strip().lower()
            role = str(item.role or "").strip().lower()
            if kind in {"reasoning_summary", "tool_use_summary", "permission_denial"}:
                rows.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "thought": str(item.summary or item.content or "").strip() or None,
                    }
                )
                continue
            if kind not in {"message", "user_message", "assistant_message", "system_message"} and role not in {
                "user",
                "assistant",
                "system",
            }:
                continue
            if role not in {"user", "assistant", "system"}:
                continue
            rows.append(
                {
                    "role": role,
                    "content": str(item.content or ""),
                    "thought": str(item.thought or "").strip() or None,
                    "metadata": dict(item.metadata or {}) if isinstance(item.metadata, dict) else {},
                }
            )
        return rows

    def canonical_active_message_rows(
        self,
        *,
        fallback_boundary_message_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        canonical = self.canonical_history(
            fallback_boundary_message_id=fallback_boundary_message_id,
        )
        rows: List[Dict[str, Any]] = []
        for item in canonical.active_entries:
            kind = str(item.kind or "").strip().lower()
            role = str(item.role or "").strip().lower()
            if kind in {"reasoning_summary", "tool_use_summary", "permission_denial"}:
                rows.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "thought": str(item.summary or item.content or "").strip() or None,
                    }
                )
                continue
            if kind not in {"message", "user_message", "assistant_message", "system_message"} and role not in {
                "user",
                "assistant",
                "system",
            }:
                continue
            if role not in {"user", "assistant", "system"}:
                continue
            rows.append(
                {
                    "role": role,
                    "content": str(item.content or ""),
                    "thought": str(item.thought or "").strip() or None,
                    "metadata": dict(item.metadata or {}) if isinstance(item.metadata, dict) else {},
                }
            )
        return rows

    def canonical_message_entries(
        self,
        *,
        fallback_boundary_message_id: Optional[int] = None,
    ) -> List[ConversationItemEntry]:
        canonical = self.canonical_history(
            fallback_boundary_message_id=fallback_boundary_message_id,
        )
        return [item for item in canonical.active_entries if self._is_message_like(item)]

    def replay(self) -> List[Dict[str, Any]]:
        return [asdict(entry) for entry in self.entries]

    def to_payload(self) -> Dict[str, Any]:
        return {
            "version": "conversation_item_stream.v1",
            "updated_at": self.updated_at or datetime.utcnow().isoformat(),
            "entries": self.replay(),
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any] | None) -> "ConversationItemStreamStore":
        entries: List[ConversationItemEntry] = []
        updated_at: Optional[str] = None
        if isinstance(payload, dict):
            updated_at = str(payload.get("updated_at") or "").strip() or None
            for item in list(payload.get("entries") or []):
                if not isinstance(item, dict):
                    continue
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
                entries.append(
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
                        created_at=str(item.get("created_at") or "").strip() or None,
                    )
                )
        return cls(entries=entries, updated_at=updated_at)


def build_context_snapshot_payload(
    *,
    mode: str,
    context_state: Dict[str, Any],
    compacted_history: Dict[str, Any],
    summary_text: str,
    compacted_message_count: int,
    up_to_message_id: Optional[int],
) -> Dict[str, Any]:
    return {
        "version": "conversation_context_snapshot.v1",
        "mode": str(mode or "auto"),
        "created_at": datetime.utcnow().isoformat(),
        "summary_text": str(summary_text or "").strip(),
        "compacted_message_count": int(max(0, compacted_message_count)),
        "up_to_message_id": int(up_to_message_id) if up_to_message_id is not None else None,
        "context_state": dict(context_state or {}),
        "compacted_history": dict(compacted_history or {}),
    }
