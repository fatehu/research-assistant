import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

from loguru import logger


ChatRunPublishFn = Callable[[str, Any], Awaitable[None]]
ChatRunExecuteFn = Callable[[ChatRunPublishFn], Awaitable[Dict[str, Any]]]
ChatRunPersistEventFn = Callable[[Dict[str, Any]], Awaitable[None]]


@dataclass
class ChatBackgroundRunRecord:
    run_id: str
    user_id: int
    status: str = "running"
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    completed_at: Optional[str] = None
    error: Optional[str] = None
    result: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)
    persist_event_fn: Optional[ChatRunPersistEventFn] = field(default=None, repr=False, compare=False)
    task: Optional[asyncio.Task] = field(default=None, repr=False, compare=False)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "user_id": self.user_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "result": dict(self.result or {}),
            "event_count": len(self.events),
        }


class ChatBackgroundRunManager:
    def __init__(self) -> None:
        self._records: Dict[str, ChatBackgroundRunRecord] = {}
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        *,
        run_id: str,
        user_id: int,
        execute_fn: ChatRunExecuteFn,
        persist_event_fn: Optional[ChatRunPersistEventFn] = None,
    ) -> Dict[str, Any]:
        record = ChatBackgroundRunRecord(
            run_id=str(run_id),
            user_id=int(user_id),
            persist_event_fn=persist_event_fn,
        )
        async with self._lock:
            self._records[record.run_id] = record
            self._subscribers.setdefault(record.run_id, [])
        record.task = asyncio.create_task(self._run(record, execute_fn), name=f"chat-background-run:{record.run_id}")
        return record.snapshot()

    async def get(self, run_id: str, *, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        async with self._lock:
            record = self._records.get(str(run_id or "").strip())
            if record is None:
                return None
            if user_id is not None and int(record.user_id) != int(user_id):
                return None
            return record.snapshot()

    async def cancel(self, run_id: str, *, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        async with self._lock:
            record = self._records.get(str(run_id or "").strip())
            if record is None:
                return None
            if user_id is not None and int(record.user_id) != int(user_id):
                return None
            task = record.task
        if task is not None and not task.done():
            task.cancel()
        return await self.get(run_id, user_id=user_id)

    async def publish(self, run_id: str, event: str, data: Any) -> None:
        payload = {
            "event": str(event or ""),
            "data": data,
            "created_at": datetime.utcnow().isoformat(),
        }
        async with self._lock:
            record = self._records.get(str(run_id or "").strip())
            if record is None:
                return
            record.events.append(payload)
            record.events = record.events[-500:]
            record.updated_at = payload["created_at"]
            persist_event_fn = record.persist_event_fn
            subscribers = list(self._subscribers.get(record.run_id) or [])
        if persist_event_fn is not None:
            try:
                await persist_event_fn(dict(payload))
            except Exception as exc:
                logger.warning(f"Failed to persist chat background run event: run_id={run_id}, event={event}, error={exc}")
        for queue in subscribers:
            await queue.put(dict(payload))

    async def subscribe(self, run_id: str, *, user_id: int, replay: bool = True):
        normalized_run_id = str(run_id or "").strip()
        queue: asyncio.Queue = asyncio.Queue()
        async with self._lock:
            record = self._records.get(normalized_run_id)
            if record is None or int(record.user_id) != int(user_id):
                return
            replay_events = list(record.events or []) if replay else []
            self._subscribers.setdefault(normalized_run_id, []).append(queue)

        try:
            for event in replay_events:
                yield dict(event)
            if replay_events and str(replay_events[-1].get("event") or "") in {"done", "error", "cancelled"}:
                return
            while True:
                event = await queue.get()
                yield dict(event)
                if str(event.get("event") or "") in {"done", "error", "cancelled"}:
                    return
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(normalized_run_id) or []
                self._subscribers[normalized_run_id] = [item for item in subscribers if item is not queue]

    async def _run(self, record: ChatBackgroundRunRecord, execute_fn: ChatRunExecuteFn) -> None:
        try:
            record.status = "running"
            result = await execute_fn(lambda event, data: self.publish(record.run_id, event, data))
            record.result = dict(result or {})
            record.status = "completed"
            record.completed_at = datetime.utcnow().isoformat()
            if not record.events or str(record.events[-1].get("event") or "") not in {"done", "error", "cancelled"}:
                await self.publish(record.run_id, "done", record.result)
        except asyncio.CancelledError:
            record.status = "cancelled"
            record.completed_at = datetime.utcnow().isoformat()
            await self.publish(record.run_id, "cancelled", {"run_id": record.run_id})
            raise
        except Exception as exc:
            record.status = "failed"
            record.error = str(exc)
            record.completed_at = datetime.utcnow().isoformat()
            await self.publish(record.run_id, "error", str(exc))
        finally:
            record.updated_at = datetime.utcnow().isoformat()


_chat_background_run_manager = ChatBackgroundRunManager()


def get_chat_background_run_manager() -> ChatBackgroundRunManager:
    return _chat_background_run_manager
