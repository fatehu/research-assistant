"""
Startup warmup for retrieval runtime components.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

from loguru import logger

from app.config import settings
from app.services.embedding_service import EmbeddingService, get_embedding_service
from app.services.reranker_service import RerankerService, get_reranker_service


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RetrievalWarmupComponentReport:
    component: str
    status: str
    duration_ms: float
    detail: str = ""
    metadata: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "component": self.component,
            "status": self.status,
            "duration_ms": round(float(self.duration_ms), 2),
            "detail": self.detail,
        }
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


class RetrievalWarmupService:
    """Manage retrieval runtime warmup and expose a live status snapshot."""

    _TERMINAL_OVERALL_STATUSES = frozenset({"ready", "degraded", "disabled", "skipped"})

    def __init__(
        self,
        *,
        embedding_factory: Callable[[], EmbeddingService] = get_embedding_service,
        reranker_factory: Callable[[], RerankerService] = get_reranker_service,
    ):
        self._embedding_factory = embedding_factory
        self._reranker_factory = reranker_factory
        self._state_lock = threading.Lock()
        self._background_task: Optional[asyncio.Task[None]] = None
        self._status_snapshot = self._build_idle_snapshot()

    @staticmethod
    def _resolve_timeout_seconds() -> float:
        return max(1.0, float(getattr(settings, "retrieval_warmup_timeout_seconds", 180) or 180))

    def _resolve_late_ready_timeout_seconds(self) -> float:
        return max(5.0, self._resolve_timeout_seconds())

    @staticmethod
    def _resolve_late_ready_poll_interval_seconds() -> float:
        return 2.0

    @staticmethod
    def _component_snapshot(
        component: str,
        *,
        status: str,
        detail: str = "",
        duration_ms: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "component": component,
            "status": status,
            "detail": detail,
            "duration_ms": round(float(duration_ms), 2),
        }
        if metadata:
            payload["metadata"] = dict(metadata)
        return payload

    def _build_idle_snapshot(self) -> Dict[str, Any]:
        timeout_seconds = self._resolve_timeout_seconds()
        enabled = bool(getattr(settings, "retrieval_warmup_on_startup", True))
        return {
            "enabled": enabled,
            "status": "idle" if enabled else "disabled",
            "timeout_seconds": timeout_seconds,
            "duration_ms": 0.0,
            "started_at": None,
            "completed_at": None,
            "background_task_running": False,
            "components": [
                self._component_snapshot("embedding", status="idle", detail="not started"),
                self._component_snapshot("reranker", status="idle", detail="not started"),
            ],
        }

    def _set_snapshot(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        stored = copy.deepcopy(snapshot)
        with self._state_lock:
            self._status_snapshot = stored
        return copy.deepcopy(stored)

    def _update_snapshot(self, updater: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
        with self._state_lock:
            next_snapshot = copy.deepcopy(self._status_snapshot)
            updater(next_snapshot)
            self._status_snapshot = next_snapshot
            return copy.deepcopy(next_snapshot)

    def _find_component(self, snapshot: Dict[str, Any], component: str) -> Dict[str, Any]:
        for item in snapshot.get("components") or []:
            if str(item.get("component") or "").strip() == component:
                return item
        item = self._component_snapshot(component, status="idle", detail="not started")
        snapshot.setdefault("components", []).append(item)
        return item

    def get_status_snapshot(self) -> Dict[str, Any]:
        with self._state_lock:
            snapshot = copy.deepcopy(self._status_snapshot)
            task = self._background_task
        snapshot["background_task_running"] = bool(task and not task.done())
        return snapshot

    @staticmethod
    def _component_timed_out(report: RetrievalWarmupComponentReport) -> bool:
        return bool(isinstance(report.metadata, dict) and report.metadata.get("timeout_exceeded"))

    @staticmethod
    def _runtime_ready(payload: Dict[str, Any]) -> bool:
        return bool((payload or {}).get("ready"))

    @staticmethod
    def _get_component_runtime_status(component_runtime: object) -> Dict[str, Any]:
        runtime_getter = getattr(component_runtime, "get_runtime_status", None)
        if not callable(runtime_getter):
            return {"ready": False}
        try:
            payload = runtime_getter()
        except Exception as exc:
            logger.warning("[RetrievalWarmup] runtime status probe failed: {}", exc)
            return {"ready": False, "probe_error": str(exc)}
        if isinstance(payload, dict):
            return payload
        return {"ready": False}

    async def _await_component_ready_after_timeout(
        self,
        component: str,
        component_runtime: object,
        timed_out_report: RetrievalWarmupComponentReport,
    ) -> RetrievalWarmupComponentReport:
        late_timeout_seconds = self._resolve_late_ready_timeout_seconds()
        poll_interval_seconds = self._resolve_late_ready_poll_interval_seconds()
        timeout_seconds = self._resolve_timeout_seconds()
        started_at = time.perf_counter()

        while (time.perf_counter() - started_at) < late_timeout_seconds:
            runtime_status = self._get_component_runtime_status(component_runtime)
            if self._runtime_ready(runtime_status):
                report = RetrievalWarmupComponentReport(
                    component=component,
                    status="warmed",
                    detail="runtime ready after startup timeout",
                    duration_ms=timed_out_report.duration_ms + ((time.perf_counter() - started_at) * 1000),
                    metadata={**runtime_status, "late_ready": True},
                )
                logger.info(
                    "[RetrievalWarmup] component={} status={} detail={} metadata={}",
                    component,
                    report.status,
                    report.detail,
                    report.metadata or {},
                )
                return report
            await asyncio.sleep(poll_interval_seconds)

        runtime_status = self._get_component_runtime_status(component_runtime)
        return RetrievalWarmupComponentReport(
            component=component,
            status="timeout",
            detail=(
                f"warmup exceeded {timeout_seconds:.1f}s and did not report ready within "
                f"{late_timeout_seconds:.1f}s"
            ),
            duration_ms=timed_out_report.duration_ms + (late_timeout_seconds * 1000),
            metadata=runtime_status if isinstance(runtime_status, dict) else None,
        )

    async def _run_component(
        self,
        component: str,
        runner: Callable[[], Awaitable[Dict[str, Any]]],
    ) -> RetrievalWarmupComponentReport:
        started_at = time.perf_counter()
        timeout_seconds = self._resolve_timeout_seconds()
        try:
            payload = await asyncio.wait_for(runner(), timeout=timeout_seconds)
            status = str((payload or {}).get("status") or "warmed").strip() or "warmed"
            detail = str((payload or {}).get("detail") or "").strip()
            metadata = (payload or {}).get("metadata")
            report = RetrievalWarmupComponentReport(
                component=component,
                status=status,
                detail=detail,
                metadata=metadata if isinstance(metadata, dict) else None,
                duration_ms=(time.perf_counter() - started_at) * 1000,
            )
        except asyncio.TimeoutError:
            report = RetrievalWarmupComponentReport(
                component=component,
                status="warming",
                detail=f"warmup exceeded {timeout_seconds:.1f}s; waiting for runtime ready signal",
                duration_ms=(time.perf_counter() - started_at) * 1000,
                metadata={"timeout_exceeded": True},
            )
        except Exception as exc:
            report = RetrievalWarmupComponentReport(
                component=component,
                status="failed",
                detail=str(exc),
                duration_ms=(time.perf_counter() - started_at) * 1000,
                metadata={"exception_type": type(exc).__name__},
            )

        logger.info(
            "[RetrievalWarmup] component={} status={} duration_ms={} detail={} metadata={}",
            report.component,
            report.status,
            round(report.duration_ms, 2),
            report.detail or "-",
            report.metadata or {},
        )
        return report

    async def _execute_warmup(self) -> Dict[str, Any]:
        enabled = bool(getattr(settings, "retrieval_warmup_on_startup", True))
        timeout_seconds = self._resolve_timeout_seconds()
        logger.info(
            "[RetrievalWarmup] startup enabled={} timeout_seconds={}",
            enabled,
            timeout_seconds,
        )

        if not enabled:
            return self._set_snapshot(
                {
                    "enabled": False,
                    "status": "disabled",
                    "timeout_seconds": timeout_seconds,
                    "duration_ms": 0.0,
                    "started_at": None,
                    "completed_at": _utc_now_iso(),
                    "background_task_running": False,
                    "components": [
                        self._component_snapshot("embedding", status="disabled", detail="startup warmup disabled"),
                        self._component_snapshot("reranker", status="disabled", detail="startup warmup disabled"),
                    ],
                }
            )

        started_at = time.perf_counter()
        started_at_iso = _utc_now_iso()
        self._set_snapshot(
            {
                "enabled": True,
                "status": "warming",
                "timeout_seconds": timeout_seconds,
                "duration_ms": 0.0,
                "started_at": started_at_iso,
                "completed_at": None,
                "background_task_running": True,
                "components": [
                    self._component_snapshot("embedding", status="queued", detail="waiting to start"),
                    self._component_snapshot("reranker", status="queued", detail="waiting to start"),
                ],
            }
        )

        component_runtimes = [
            ("embedding", self._embedding_factory()),
            ("reranker", self._reranker_factory()),
        ]
        reports: list[RetrievalWarmupComponentReport] = []

        for component, component_runtime in component_runtimes:
            self._update_snapshot(
                lambda snapshot, component=component: self._find_component(snapshot, component).update(
                    {
                        "status": "warming",
                        "detail": "warming in background",
                        "duration_ms": 0.0,
                    }
                )
            )
            report = await self._run_component(component, component_runtime.warmup)
            reports.append(report)
            self._update_snapshot(
                lambda snapshot, report=report: self._find_component(snapshot, report.component).update(report.as_dict())
            )

        late_ready_candidates = [
            (index, component, component_runtime, report)
            for index, ((component, component_runtime), report) in enumerate(zip(component_runtimes, reports))
            if self._component_timed_out(report)
        ]

        for index, component, component_runtime, timed_out_report in late_ready_candidates:
            resolved_report = await self._await_component_ready_after_timeout(
                component,
                component_runtime,
                timed_out_report,
            )
            reports[index] = resolved_report
            self._update_snapshot(
                lambda snapshot, report=resolved_report: self._find_component(snapshot, report.component).update(
                    report.as_dict()
                )
            )

        statuses = [report.status for report in reports]
        if any(status in {"failed", "timeout"} for status in statuses):
            overall_status = "degraded"
        elif all(status == "skipped" for status in statuses):
            overall_status = "skipped"
        else:
            overall_status = "ready"

        report = self._set_snapshot(
            {
                "enabled": True,
                "status": overall_status,
                "timeout_seconds": timeout_seconds,
                "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
                "started_at": started_at_iso,
                "completed_at": _utc_now_iso(),
                "background_task_running": False,
                "components": [item.as_dict() for item in reports],
            }
        )
        logger.info(
            "[RetrievalWarmup] overall_status={} duration_ms={} components={}",
            report["status"],
            report["duration_ms"],
            report["components"],
        )
        return report

    async def _background_runner(self) -> None:
        try:
            report = await self._execute_warmup()
            logger.info(
                "[RetrievalWarmupBackground] status={} duration_ms={} components={}",
                report.get("status"),
                report.get("duration_ms"),
                report.get("components") or [],
            )
        except asyncio.CancelledError:
            self._update_snapshot(
                lambda snapshot: snapshot.update(
                    {
                        "status": "idle" if snapshot.get("enabled") else "disabled",
                        "completed_at": _utc_now_iso(),
                        "background_task_running": False,
                    }
                )
            )
            raise
        except Exception as exc:
            logger.exception("[RetrievalWarmupBackground] unexpected failure: {}", exc)
            self._set_snapshot(
                {
                    "enabled": True,
                    "status": "degraded",
                    "timeout_seconds": self._resolve_timeout_seconds(),
                    "duration_ms": 0.0,
                    "started_at": None,
                    "completed_at": _utc_now_iso(),
                    "background_task_running": False,
                    "components": [
                        self._component_snapshot("embedding", status="failed", detail="background warmup aborted"),
                        self._component_snapshot("reranker", status="failed", detail=str(exc)),
                    ],
                }
            )

    def start_background_warmup(self) -> Dict[str, Any]:
        enabled = bool(getattr(settings, "retrieval_warmup_on_startup", True))
        timeout_seconds = self._resolve_timeout_seconds()

        if not enabled:
            return self._set_snapshot(
                {
                    "enabled": False,
                    "status": "disabled",
                    "timeout_seconds": timeout_seconds,
                    "duration_ms": 0.0,
                    "started_at": None,
                    "completed_at": _utc_now_iso(),
                    "background_task_running": False,
                    "components": [
                        self._component_snapshot("embedding", status="disabled", detail="startup warmup disabled"),
                        self._component_snapshot("reranker", status="disabled", detail="startup warmup disabled"),
                    ],
                }
            )

        with self._state_lock:
            if self._background_task and not self._background_task.done():
                snapshot = copy.deepcopy(self._status_snapshot)
                snapshot["background_task_running"] = True
                return snapshot

            initial_snapshot = {
                "enabled": True,
                "status": "warming",
                "timeout_seconds": timeout_seconds,
                "duration_ms": 0.0,
                "started_at": _utc_now_iso(),
                "completed_at": None,
                "background_task_running": True,
                "components": [
                    self._component_snapshot("embedding", status="queued", detail="waiting to start"),
                    self._component_snapshot("reranker", status="queued", detail="waiting to start"),
                ],
            }
            self._status_snapshot = copy.deepcopy(initial_snapshot)
            self._background_task = asyncio.create_task(self._background_runner(), name="retrieval-warmup")
            return copy.deepcopy(initial_snapshot)

    async def warmup_on_startup(self) -> Dict[str, Any]:
        return await self._execute_warmup()

    async def wait_for_background_warmup(self, timeout_seconds: float = 5.0) -> Dict[str, Any]:
        with self._state_lock:
            task = self._background_task
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
        return self.get_status_snapshot()

    async def shutdown(self) -> None:
        with self._state_lock:
            task = self._background_task
            self._background_task = None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


_retrieval_warmup_service = RetrievalWarmupService()


def get_retrieval_warmup_service() -> RetrievalWarmupService:
    return _retrieval_warmup_service
