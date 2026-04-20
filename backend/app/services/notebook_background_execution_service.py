from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional


BackgroundExecuteFn = Callable[[], Dict[str, Any]]
BackgroundCancelFn = Callable[[], None]
BackgroundFinalizeFn = Callable[[Dict[str, Any], Optional[Dict[str, Any]]], Awaitable[None]]


@dataclass
class NotebookBackgroundExecutionRecord:
    execution_id: str
    notebook_id: str
    user_id: int
    cell_id: str
    description: str = ""
    status: str = "pending"
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    cancel_requested: bool = False
    success: Optional[bool] = None
    terminated_reason: Optional[str] = None
    policy_violation_code: Optional[str] = None
    execution_count: Optional[int] = None
    error: Optional[str] = None
    task: Optional[asyncio.Task[None]] = field(default=None, repr=False, compare=False)
    cancel_fn: Optional[BackgroundCancelFn] = field(default=None, repr=False, compare=False)
    result_payload: Optional[Dict[str, Any]] = field(default=None, repr=False, compare=False)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "notebook_id": self.notebook_id,
            "user_id": self.user_id,
            "cell_id": self.cell_id,
            "description": self.description,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "cancel_requested": self.cancel_requested,
            "success": self.success,
            "terminated_reason": self.terminated_reason,
            "policy_violation_code": self.policy_violation_code,
            "execution_count": self.execution_count,
            "error": self.error,
        }


class NotebookBackgroundExecutionBusyError(RuntimeError):
    pass


class NotebookBackgroundExecutionManager:
    def __init__(self) -> None:
        self._records: Dict[str, NotebookBackgroundExecutionRecord] = {}
        self._active_by_notebook: Dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        *,
        notebook_id: str,
        user_id: int,
        cell_id: str,
        description: str,
        execute_fn: BackgroundExecuteFn,
        cancel_fn: Optional[BackgroundCancelFn],
        finalize_fn: BackgroundFinalizeFn,
    ) -> Dict[str, Any]:
        async with self._lock:
            active_execution_id = self._active_by_notebook.get(notebook_id)
            if active_execution_id:
                active_record = self._records.get(active_execution_id)
                if active_record and active_record.status in {"pending", "running"}:
                    raise NotebookBackgroundExecutionBusyError(
                        f"notebook background execution already running: {active_execution_id}"
                    )

            record = NotebookBackgroundExecutionRecord(
                execution_id=str(uuid.uuid4()),
                notebook_id=notebook_id,
                user_id=int(user_id),
                cell_id=cell_id,
                description=str(description or "").strip(),
                cancel_fn=cancel_fn,
            )
            self._records[record.execution_id] = record
            self._active_by_notebook[notebook_id] = record.execution_id

        record.task = asyncio.create_task(self._run(record, execute_fn=execute_fn, finalize_fn=finalize_fn))
        return record.snapshot()

    async def cancel(
        self,
        *,
        execution_id: str,
        user_id: Optional[int] = None,
        notebook_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        async with self._lock:
            record = self._records.get(str(execution_id or "").strip())
            if record is None:
                return None
            if user_id is not None and int(user_id) != int(record.user_id):
                return None
            if notebook_id is not None and str(notebook_id) != str(record.notebook_id):
                return None
            record.cancel_requested = True
            cancel_fn = record.cancel_fn

        if callable(cancel_fn):
            cancel_fn()
        return record.snapshot()

    async def get(
        self,
        *,
        execution_id: str,
        user_id: Optional[int] = None,
        notebook_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        async with self._lock:
            record = self._records.get(str(execution_id or "").strip())
            if record is None:
                return None
            if user_id is not None and int(user_id) != int(record.user_id):
                return None
            if notebook_id is not None and str(notebook_id) != str(record.notebook_id):
                return None
            return record.snapshot()

    async def list_notebook(self, *, notebook_id: str, user_id: Optional[int] = None) -> list[Dict[str, Any]]:
        async with self._lock:
            snapshots = []
            for record in self._records.values():
                if str(record.notebook_id) != str(notebook_id):
                    continue
                if user_id is not None and int(user_id) != int(record.user_id):
                    continue
                snapshots.append(record.snapshot())
            snapshots.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
            return snapshots

    async def wait(
        self,
        *,
        execution_id: str,
        timeout_seconds: float,
        user_id: Optional[int] = None,
        notebook_id: Optional[str] = None,
        include_result: bool = False,
    ) -> Optional[Dict[str, Any]]:
        normalized_execution_id = str(execution_id or "").strip()
        async with self._lock:
            record = self._records.get(normalized_execution_id)
            if record is None:
                return None
            if user_id is not None and int(user_id) != int(record.user_id):
                return None
            if notebook_id is not None and str(notebook_id) != str(record.notebook_id):
                return None
            task = record.task

        if task is not None and not task.done() and float(timeout_seconds or 0) > 0:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=float(timeout_seconds))
            except asyncio.TimeoutError:
                pass

        async with self._lock:
            record = self._records.get(normalized_execution_id)
            if record is None:
                return None
            if user_id is not None and int(user_id) != int(record.user_id):
                return None
            if notebook_id is not None and str(notebook_id) != str(record.notebook_id):
                return None
            snapshot = record.snapshot()
            if include_result:
                snapshot["result_payload"] = record.result_payload
            return snapshot

    async def _run(
        self,
        record: NotebookBackgroundExecutionRecord,
        *,
        execute_fn: BackgroundExecuteFn,
        finalize_fn: BackgroundFinalizeFn,
    ) -> None:
        result: Optional[Dict[str, Any]] = None
        try:
            record.status = "running"
            record.started_at = datetime.utcnow().isoformat()
            result = await asyncio.to_thread(execute_fn)
            record.result_payload = result
            record.success = bool((result or {}).get("success"))
            record.terminated_reason = str((result or {}).get("terminated_reason") or "").strip() or None
            record.policy_violation_code = str((result or {}).get("policy_violation_code") or "").strip() or None
            try:
                record.execution_count = int((result or {}).get("execution_count"))
            except Exception:
                record.execution_count = None

            if record.cancel_requested or record.terminated_reason == "cancelled":
                record.status = "cancelled"
            elif record.success:
                record.status = "completed"
            else:
                record.status = "failed"
                record.error = str((result or {}).get("error") or "").strip() or None
        except Exception as exc:
            record.success = False
            record.error = str(exc)
            record.status = "cancelled" if record.cancel_requested else "failed"
        finally:
            record.completed_at = datetime.utcnow().isoformat()
            await finalize_fn(record.snapshot(), result)
            async with self._lock:
                active_execution_id = self._active_by_notebook.get(record.notebook_id)
                if active_execution_id == record.execution_id:
                    self._active_by_notebook.pop(record.notebook_id, None)


_background_execution_manager = NotebookBackgroundExecutionManager()


def get_notebook_background_execution_manager() -> NotebookBackgroundExecutionManager:
    return _background_execution_manager
