import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.services.retrieval_warmup_service import RetrievalWarmupService


class _WarmStub:
    def __init__(
        self,
        payload=None,
        exc: Exception | None = None,
        delay_seconds: float = 0.0,
        ready_after_seconds: float | None = None,
    ):
        self.payload = payload or {"status": "warmed", "detail": "ok", "metadata": {"name": "stub"}}
        self.exc = exc
        self.delay_seconds = delay_seconds
        self.ready_after_seconds = ready_after_seconds
        self.calls = 0
        self.started_at: float | None = None

    async def warmup(self):
        self.calls += 1
        if self.started_at is None:
            self.started_at = time.perf_counter()
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)
        if self.exc is not None:
            raise self.exc
        return self.payload

    def get_runtime_status(self):
        ready = False
        if self.ready_after_seconds is not None and self.started_at is not None:
            ready = (time.perf_counter() - self.started_at) >= self.ready_after_seconds
        return {"ready": ready}


@pytest.mark.asyncio
async def test_retrieval_warmup_service_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "retrieval_warmup_on_startup", False)
    monkeypatch.setattr(settings, "retrieval_warmup_timeout_seconds", 5)
    embedding = _WarmStub()
    reranker = _WarmStub()
    service = RetrievalWarmupService(
        embedding_factory=lambda: embedding,
        reranker_factory=lambda: reranker,
    )

    report = await service.warmup_on_startup()

    assert report["enabled"] is False
    assert report["status"] == "disabled"
    assert [item["component"] for item in report["components"]] == ["embedding", "reranker"]
    assert [item["status"] for item in report["components"]] == ["disabled", "disabled"]
    assert embedding.calls == 0
    assert reranker.calls == 0


@pytest.mark.asyncio
async def test_retrieval_warmup_service_reports_ready_when_components_warm(monkeypatch):
    monkeypatch.setattr(settings, "retrieval_warmup_on_startup", True)
    monkeypatch.setattr(settings, "retrieval_warmup_timeout_seconds", 5)
    embedding = _WarmStub(payload={"status": "warmed", "detail": "embedding ready", "metadata": {"model": "bge"}})
    reranker = _WarmStub(payload={"status": "warmed", "detail": "reranker ready", "metadata": {"model": "gte"}})
    service = RetrievalWarmupService(
        embedding_factory=lambda: embedding,
        reranker_factory=lambda: reranker,
    )

    report = await service.warmup_on_startup()

    assert report["enabled"] is True
    assert report["status"] == "ready"
    assert [item["component"] for item in report["components"]] == ["embedding", "reranker"]
    assert [item["status"] for item in report["components"]] == ["warmed", "warmed"]
    assert embedding.calls == 1
    assert reranker.calls == 1


@pytest.mark.asyncio
async def test_retrieval_warmup_service_marks_degraded_on_timeout(monkeypatch):
    monkeypatch.setattr(settings, "retrieval_warmup_on_startup", True)
    monkeypatch.setattr(settings, "retrieval_warmup_timeout_seconds", 5)
    embedding = _WarmStub(delay_seconds=0.05)
    reranker = _WarmStub(payload={"status": "skipped", "detail": "reranker disabled", "metadata": {}})
    service = RetrievalWarmupService(
        embedding_factory=lambda: embedding,
        reranker_factory=lambda: reranker,
    )
    monkeypatch.setattr(service, "_resolve_timeout_seconds", lambda: 0.01)
    monkeypatch.setattr(service, "_resolve_late_ready_timeout_seconds", lambda: 0.01)
    monkeypatch.setattr(service, "_resolve_late_ready_poll_interval_seconds", lambda: 0.005)

    report = await service.warmup_on_startup()

    assert report["enabled"] is True
    assert report["status"] == "degraded"
    assert report["components"][0]["component"] == "embedding"
    assert report["components"][0]["status"] == "timeout"
    assert report["components"][1]["component"] == "reranker"
    assert report["components"][1]["status"] == "skipped"


@pytest.mark.asyncio
async def test_retrieval_warmup_service_updates_timeout_to_ready_when_runtime_finishes(monkeypatch):
    monkeypatch.setattr(settings, "retrieval_warmup_on_startup", True)
    monkeypatch.setattr(settings, "retrieval_warmup_timeout_seconds", 5)
    embedding = _WarmStub(delay_seconds=0.05, ready_after_seconds=0.03)
    reranker = _WarmStub(payload={"status": "warmed", "detail": "reranker ready", "metadata": {"model": "gte"}})
    service = RetrievalWarmupService(
        embedding_factory=lambda: embedding,
        reranker_factory=lambda: reranker,
    )
    monkeypatch.setattr(service, "_resolve_timeout_seconds", lambda: 0.01)
    monkeypatch.setattr(service, "_resolve_late_ready_timeout_seconds", lambda: 0.08)
    monkeypatch.setattr(service, "_resolve_late_ready_poll_interval_seconds", lambda: 0.005)

    report = await service.warmup_on_startup()

    assert report["enabled"] is True
    assert report["status"] == "ready"
    assert report["components"][0]["component"] == "embedding"
    assert report["components"][0]["status"] == "warmed"
    assert report["components"][0]["metadata"]["late_ready"] is True
    assert report["components"][1]["component"] == "reranker"
    assert report["components"][1]["status"] == "warmed"


@pytest.mark.asyncio
async def test_retrieval_warmup_service_can_run_in_background(monkeypatch):
    monkeypatch.setattr(settings, "retrieval_warmup_on_startup", True)
    monkeypatch.setattr(settings, "retrieval_warmup_timeout_seconds", 5)
    embedding = _WarmStub(delay_seconds=0.01)
    reranker = _WarmStub(delay_seconds=0.01)
    service = RetrievalWarmupService(
        embedding_factory=lambda: embedding,
        reranker_factory=lambda: reranker,
    )

    initial = service.start_background_warmup()

    assert initial["enabled"] is True
    assert initial["status"] == "warming"
    assert initial["background_task_running"] is True
    assert [item["status"] for item in initial["components"]] == ["queued", "queued"]

    final_report = await service.wait_for_background_warmup(timeout_seconds=1)

    assert final_report["status"] == "ready"
    assert final_report["background_task_running"] is False
    assert [item["status"] for item in final_report["components"]] == ["warmed", "warmed"]
    assert embedding.calls == 1
    assert reranker.calls == 1
