from __future__ import annotations

import asyncio
import io
import os
import sys
from types import SimpleNamespace

from fastapi import BackgroundTasks
from starlette.datastructures import Headers, UploadFile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api import knowledge as knowledge_api
from app.models.knowledge import Document, DocumentStatus, KnowledgeBase


class _FakeUploadDB:
    def __init__(self, *, kb: KnowledgeBase) -> None:
        self.kb = kb
        self.added: list[Document] = []
        self.commit_calls = 0
        self.rollback_calls = 0

    async def get(self, model, record_id):
        if model is KnowledgeBase and int(record_id) == int(self.kb.id):
            return self.kb
        return None

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for index, obj in enumerate(self.added, start=1):
            if getattr(obj, "id", None) is None:
                obj.id = index

    async def commit(self):
        self.commit_calls += 1
        for index, obj in enumerate(self.added, start=1):
            if getattr(obj, "id", None) is None:
                obj.id = index

    async def rollback(self):
        self.rollback_calls += 1

    async def refresh(self, _obj):
        return None

    async def execute(self, _stmt):
        document_count = len([obj for obj in self.added if isinstance(obj, Document)])
        total_chunks = sum(int(getattr(obj, "chunk_count", 0) or 0) for obj in self.added if isinstance(obj, Document))
        total_tokens = sum(int(getattr(obj, "token_count", 0) or 0) for obj in self.added if isinstance(obj, Document))
        return _FakeExecuteResult([(document_count, total_chunks, total_tokens)])


class _FakeProcessDB:
    def __init__(self, *, doc: Document, kb: KnowledgeBase) -> None:
        self.doc = doc
        self.kb = kb
        self.commit_calls = 0
        self.rollback_calls = 0

    async def get(self, model, record_id):
        if model is Document and int(record_id) == int(self.doc.id):
            return self.doc
        if model is KnowledgeBase and int(record_id) == int(self.kb.id):
            return self.kb
        return None

    async def commit(self):
        self.commit_calls += 1

    async def rollback(self):
        self.rollback_calls += 1

    async def refresh(self, _obj):
        return None


class _FakeExecuteResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def one(self):
        if not self._rows:
            return (0, 0, 0)
        first = self._rows[0]
        return first if isinstance(first, tuple) else tuple(first)


class _FakeRecoveryDB:
    def __init__(self, *, rows):
        self.rows = list(rows)
        self.commit_calls = 0

    async def execute(self, _stmt):
        return _FakeExecuteResult(self.rows)

    async def commit(self):
        self.commit_calls += 1


class _FakeSessionContext:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeOnlineMmService:
    def __init__(self, result: dict):
        self._result = result
        self.calls: list[dict[str, object]] = []

    async def ingest_pdf(
        self,
        *,
        file_path: str,
        document_name: str,
        extract_profile: str = "general",
        extract_granularity: str = "medium",
    ):
        self.calls.append(
            {
                "file_path": file_path,
                "document_name": document_name,
                "extract_profile": extract_profile,
                "extract_granularity": extract_granularity,
            }
        )
        return dict(self._result)


def _build_upload_file(name: str, content: bytes, content_type: str = "application/pdf") -> UploadFile:
    return UploadFile(file=io.BytesIO(content), filename=name, headers=Headers({"content-type": content_type}))


def test_upload_document_should_persist_ingest_request_metadata(tmp_path, monkeypatch):
    kb = KnowledgeBase(id=84, user_id=7, name="KB", chunk_size=500, chunk_overlap=50)
    db = _FakeUploadDB(kb=kb)

    async def _noop_publish(**_kwargs):
        return None

    monkeypatch.setattr(knowledge_api, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(knowledge_api, "_publish_document_status_event", _noop_publish)
    monkeypatch.setattr(knowledge_api.settings, "kb_online_mm_ingest_enabled", True)

    response = asyncio.run(
        knowledge_api.upload_document(
            kb_id=84,
            background_tasks=BackgroundTasks(),
            file=_build_upload_file("paper.pdf", b"%PDF-1.4 test"),
            ingest_mode="online_mm",
            extract_profile="academic_formula",
            extract_granularity="fine",
            db=db,
            current_user=SimpleNamespace(id=7),
        )
    )

    assert response.processing_mode == "online_mm"
    assert response.extract_profile == "academic_formula"
    assert response.extract_granularity == "fine"
    assert db.added, "expected upload to create a Document row"
    created = db.added[0]
    ingest_request = dict((created.metadata_ or {}).get("ingest_request") or {})
    assert ingest_request["mode"] == "online_mm"
    assert ingest_request["extract_profile"] == "academic_formula"
    assert ingest_request["extract_granularity"] == "fine"
    assert ingest_request["requested_by"] == 7
    assert created.original_filename == "paper.pdf"


def test_process_document_task_should_fail_directly_when_online_mm_ingest_fails(monkeypatch):
    kb = KnowledgeBase(id=84, user_id=7, name="KB", chunk_size=500, chunk_overlap=50)
    doc = Document(
        id=12,
        knowledge_base_id=84,
        filename="stored.pdf",
        original_filename="paper.pdf",
        file_path="/tmp/paper.pdf",
        file_size=128,
        file_type="pdf",
        status=DocumentStatus.PENDING.value,
        metadata_={
            "ingest_request": {
                "mode": "online_mm",
                "extract_profile": "academic_formula",
                "extract_granularity": "coarse",
                "requested_by": 7,
            }
        },
    )
    db = _FakeProcessDB(doc=doc, kb=kb)
    fake_service = _FakeOnlineMmService(
        {
            "applied": False,
            "failure_reason": "page_blocks_invalid:1:qwen3-vl-flash",
            "report": {"page": 1},
            "chunks": [],
            "document_text": "",
        }
    )

    async def _noop_publish(**_kwargs):
        return None

    monkeypatch.setattr(knowledge_api.settings, "kb_online_mm_ingest_enabled", True)
    monkeypatch.setattr(knowledge_api, "_publish_document_status_event", _noop_publish)

    import app.core.database as core_database
    import app.services.online_mm_ingest_service as online_mm_module

    monkeypatch.setattr(core_database, "async_session_factory", lambda: _FakeSessionContext(db))
    monkeypatch.setattr(online_mm_module, "get_online_mm_ingest_service", lambda: fake_service)

    asyncio.run(knowledge_api.process_document_task(doc_id=12, chunk_size=500, chunk_overlap=50))

    assert doc.status == DocumentStatus.FAILED.value
    assert "在线多模态入库失败" in str(doc.error_message or "")
    assert "page_blocks_invalid" in str(doc.error_message or "")
    online_mm_report = dict((doc.metadata_ or {}).get("online_mm_ingest") or {})
    assert online_mm_report["page"] == 1
    assert doc.processed_at is None
    assert db.commit_calls >= 2
    assert fake_service.calls == [
        {
            "file_path": "/tmp/paper.pdf",
            "document_name": "paper.pdf",
            "extract_profile": "academic_formula",
            "extract_granularity": "coarse",
        }
    ]


def test_retry_document_processing_should_queue_task(monkeypatch):
    kb = KnowledgeBase(id=84, user_id=7, name="KB", chunk_size=500, chunk_overlap=50)
    doc = Document(
        id=147,
        knowledge_base_id=84,
        filename="stored.pdf",
        original_filename="paper.pdf",
        file_path="/tmp/paper.pdf",
        file_size=128,
        file_type="pdf",
        status=DocumentStatus.FAILED.value,
        error_message="boom",
        metadata_={"online_mm_block_cache": {"blocks": [{"block_id": "p1"}]}},
    )
    db = _FakeProcessDB(doc=doc, kb=kb)

    async def _noop_publish(**_kwargs):
        return None

    monkeypatch.setattr(knowledge_api, "_publish_document_status_event", _noop_publish)
    monkeypatch.setattr(knowledge_api.os.path, "exists", lambda path: path == "/tmp/paper.pdf")
    knowledge_api._ACTIVE_DOCUMENT_TASKS.clear()

    background_tasks = BackgroundTasks()
    status = asyncio.run(
        knowledge_api.retry_document_processing(
            kb_id=84,
            doc_id=147,
            background_tasks=background_tasks,
            db=db,
            current_user=SimpleNamespace(id=7),
        )
    )

    assert status.status == DocumentStatus.PENDING.value
    assert status.message == "已加入重试队列"
    assert doc.status == DocumentStatus.PENDING.value
    assert doc.error_message is None
    retry_request = dict((doc.metadata_ or {}).get("retry_request") or {})
    assert retry_request["trigger"] == "manual"
    assert retry_request["reason"] == "manual_retry_from_cache"
    assert retry_request["count"] == 1
    assert len(background_tasks.tasks) == 1
    assert db.commit_calls >= 1


def test_resume_interrupted_document_tasks_on_startup_should_schedule_running_docs(monkeypatch):
    kb = KnowledgeBase(id=84, user_id=7, name="KB", chunk_size=700, chunk_overlap=70)
    doc = Document(
        id=147,
        knowledge_base_id=84,
        filename="stored.pdf",
        original_filename="paper.pdf",
        file_path="/tmp/paper.pdf",
        file_size=128,
        file_type="pdf",
        status=DocumentStatus.RUNNING.value,
        metadata_={"online_mm_block_cache": {"blocks": [{"block_id": "p1"}]}},
    )
    db = _FakeRecoveryDB(rows=[(doc, kb)])
    scheduled = []

    def _fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(knowledge_api, "async_session_factory", lambda: _FakeSessionContext(db))
    monkeypatch.setattr(knowledge_api.os.path, "exists", lambda path: path == "/tmp/paper.pdf")
    monkeypatch.setattr(knowledge_api.asyncio, "create_task", _fake_create_task)
    monkeypatch.setattr(knowledge_api.settings, "knowledge_resume_running_documents_on_startup", True)
    monkeypatch.setattr(knowledge_api.settings, "knowledge_resume_running_documents_limit", 5)
    knowledge_api._ACTIVE_DOCUMENT_TASKS.clear()

    report = asyncio.run(knowledge_api.resume_interrupted_document_tasks_on_startup())

    assert report["enabled"] is True
    assert report["scheduled"] == 1
    assert report["marked_failed"] == 0
    assert report["documents"] == [147]
    retry_request = dict((doc.metadata_ or {}).get("retry_request") or {})
    assert retry_request["trigger"] == "startup"
    assert retry_request["reason"] == "startup_resume_from_cache"
    assert retry_request["count"] == 1
    assert db.commit_calls == 1
    assert len(scheduled) == 1


def test_get_document_status_should_resume_interrupted_running_doc(monkeypatch):
    kb = KnowledgeBase(id=84, user_id=7, name="KB", chunk_size=640, chunk_overlap=64)
    doc = Document(
        id=147,
        knowledge_base_id=84,
        filename="stored.pdf",
        original_filename="paper.pdf",
        file_path="/tmp/paper.pdf",
        file_size=128,
        file_type="pdf",
        status=DocumentStatus.RUNNING.value,
        metadata_={"online_mm_block_cache": {"blocks": [{"block_id": "p1"}]}},
    )
    db = _FakeProcessDB(doc=doc, kb=kb)
    scheduled = []

    async def _noop_publish(**_kwargs):
        return None

    def _fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(knowledge_api, "_publish_document_status_event", _noop_publish)
    monkeypatch.setattr(knowledge_api.os.path, "exists", lambda path: path == "/tmp/paper.pdf")
    monkeypatch.setattr(knowledge_api.asyncio, "create_task", _fake_create_task)
    knowledge_api._ACTIVE_DOCUMENT_TASKS.clear()

    status = asyncio.run(
        knowledge_api.get_document_status(
            kb_id=84,
            doc_id=147,
            db=db,
            current_user=SimpleNamespace(id=7),
        )
    )

    assert status.status == DocumentStatus.PENDING.value
    assert status.message == "等待处理"
    retry_request = dict((doc.metadata_ or {}).get("retry_request") or {})
    assert retry_request["trigger"] == "status_poll"
    assert retry_request["reason"] == "status_poll_resume_from_cache"
    assert retry_request["count"] == 1
    assert len(scheduled) == 1
    assert db.commit_calls >= 1
