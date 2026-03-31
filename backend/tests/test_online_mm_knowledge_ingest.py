from __future__ import annotations

import asyncio
import io
import os
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api import knowledge as knowledge_api
from app.models.knowledge import Document, DocumentChunk, DocumentStatus, KnowledgeBase
from app.services.smart_chunking.types import ChunkLevel, ChunkMetadata, SmartChunk


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


class _FakeDeleteDB(_FakeProcessDB):
    def __init__(self, *, doc: Document, kb: KnowledgeBase) -> None:
        super().__init__(doc=doc, kb=kb)
        self.deleted: list[object] = []
        self.flush_calls = 0

    async def delete(self, obj):
        self.deleted.append(obj)
        if isinstance(obj, Document) and self.doc is obj:
            self.doc = None

    async def flush(self):
        self.flush_calls += 1

    async def execute(self, _stmt):
        return _FakeExecuteResult([(0, 0, 0)])

    async def scalar(self, _stmt):
        return 0


class _FakeProcessPipelineDB(_FakeProcessDB):
    def __init__(self, *, doc: Document, kb: KnowledgeBase) -> None:
        super().__init__(doc=doc, kb=kb)
        self.added: list[object] = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for index, obj in enumerate(self.added, start=1):
            if getattr(obj, "id", None) is None:
                obj.id = index

    async def execute(self, _stmt):
        document_rows = [obj for obj in self.added if isinstance(obj, Document)]
        total_chunks = sum(
            int(getattr(obj, "chunk_count", 0) or 0)
            for obj in document_rows
        )
        total_tokens = sum(
            int(getattr(obj, "token_count", 0) or 0)
            for obj in document_rows
        )
        return _FakeExecuteResult([(len(document_rows), total_chunks, total_tokens)])


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


class _FakeScalarResult:
    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return list(self._items)


class _FakeSnapshotExecuteResult:
    def __init__(self, items):
        self._items = list(items)

    def scalars(self):
        return _FakeScalarResult(self._items)


class _FakeSnapshotDB:
    def __init__(self, *, docs):
        self.docs = list(docs)

    async def execute(self, _stmt):
        return _FakeSnapshotExecuteResult(self.docs)


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


class _FakePdfRagService:
    def __init__(self, result: dict) -> None:
        self._result = result
        self.calls: list[dict[str, object]] = []

    async def ingest_pdf(self, *, file_path: str, document_name: str, mode: str = "fast"):
        self.calls.append(
            {
                "file_path": file_path,
                "document_name": document_name,
                "mode": mode,
            }
        )
        return dict(self._result)


def _build_upload_file(name: str, content: bytes, content_type: str = "application/pdf") -> UploadFile:
    return UploadFile(file=io.BytesIO(content), filename=name, headers=Headers({"content-type": content_type}))


def _reset_task_state() -> None:
    knowledge_api._ACTIVE_DOCUMENT_TASKS.clear()
    knowledge_api._DOCUMENT_TASK_HANDLES.clear()
    knowledge_api._DOCUMENT_TASK_CANCEL_REQUESTS.clear()
    knowledge_api._DOCUMENT_TASK_RUN_SEMAPHORE = None
    knowledge_api._DOCUMENT_TASK_RUN_SEMAPHORE_LIMIT = None


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


def test_upload_document_should_reject_duplicate_file_in_same_kb(tmp_path, monkeypatch):
    kb = KnowledgeBase(id=85, user_id=7, name="KB", chunk_size=500, chunk_overlap=50)
    db = _FakeUploadDB(kb=kb)
    duplicate_doc = Document(
        id=9,
        knowledge_base_id=85,
        filename="stored.pdf",
        original_filename="paper.pdf",
        file_size=13,
        file_type="pdf",
        status=DocumentStatus.COMPLETED.value,
        metadata_={"dedupe": {"file_sha256": "existing"}},
    )

    async def _noop_publish(**_kwargs):
        return None

    async def _fake_find_duplicate(*_args, **_kwargs):
        return duplicate_doc

    monkeypatch.setattr(knowledge_api, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(knowledge_api, "_publish_document_status_event", _noop_publish)
    monkeypatch.setattr(knowledge_api, "_find_duplicate_document_by_file_hash", _fake_find_duplicate)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            knowledge_api.upload_document(
                kb_id=85,
                file=_build_upload_file("paper.pdf", b"%PDF-1.4 test"),
                ingest_mode="local_fast",
                extract_profile="general",
                extract_granularity="medium",
                db=db,
                current_user=SimpleNamespace(id=7),
            )
        )

    assert exc_info.value.status_code == 409
    detail = dict(exc_info.value.detail or {})
    assert detail["code"] == "duplicate_file_upload"
    assert int(detail["details"]["duplicate_of_document_id"]) == 9
    assert not db.added
    assert not list(tmp_path.rglob("*"))


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


def test_process_document_task_should_route_local_pdf_to_smart_chunking(monkeypatch):
    kb = KnowledgeBase(id=91, user_id=7, name="KB", chunk_size=500, chunk_overlap=50)
    kb.metadata_ = {
        "chunking_config": {
            "strategy": "hybrid",
            "enable_hierarchical": True,
            "detect_academic_structure": True,
            "preserve_citations": True,
            "use_token_based": True,
            "base_chunk_tokens": 128,
            "overlap_tokens": 16,
            "min_semantic_tokens": 32,
            "max_semantic_tokens": 384,
        }
    }
    doc = Document(
        id=191,
        knowledge_base_id=91,
        filename="stored.pdf",
        original_filename="paper.pdf",
        file_path="/tmp/paper.pdf",
        file_size=128,
        file_type="pdf",
        status=DocumentStatus.PENDING.value,
        metadata_={
            "ingest_request": {
                "mode": "local_fast",
                "extract_profile": "general",
                "extract_granularity": "medium",
                "requested_by": 7,
            }
        },
    )
    db = _FakeProcessPipelineDB(doc=doc, kb=kb)
    pdf_rag_service = _FakePdfRagService(
        {
            "applied": True,
            "document_text": "# Intro\n\nThis text should go to smart chunking.",
            "document_source_spans": [
                {
                    "start_char": 0,
                    "end_char": 7,
                    "block_id": "h1",
                    "block_type": "heading",
                    "page_start": 1,
                    "page_end": 1,
                    "section_path": "Intro",
                },
                {
                    "start_char": 9,
                    "end_char": 45,
                    "block_id": "p1",
                    "block_type": "paragraph",
                    "page_start": 1,
                    "page_end": 1,
                    "section_path": "Intro",
                },
            ],
            "chunks": [
                {
                    "id": "legacy-1",
                    "content": "LEGACY PDF CHUNK SHOULD NOT BE USED",
                    "start_char": 0,
                    "end_char": 12,
                    "metadata": {"level": "paragraph"},
                }
            ],
            "report": {"mode": "fast"},
            "extractor": "local_structured_pdf_fast",
        }
    )

    class _FakeProcessor:
        last_embedding_svc = None

        last_pdf_extractor = None

        async def extract_text(self, _file_path, _file_type):
            raise AssertionError("local pdf path should reuse PdfRagIngestService document_text")

        async def embed_chunks(self, texts, embedding_svc=None):
            self.__class__.last_embedding_svc = embedding_svc
            return [[0.1, 0.2, 0.3] for _ in texts]

        def compute_hash(self, text):
            return f"hash:{len(text)}"

        def estimate_tokens(self, text):
            return max(1, len(str(text)) // 4)

    class _FakeSmartChunkingService:
        calls: list[dict[str, object]] = []

        def __init__(self, *args, **kwargs):
            self.embedding_svc = kwargs.get("embedding_svc")

        async def chunk_document(self, text, config, file_type):
            self.__class__.calls.append(
                {
                    "text": text,
                    "strategy": config.strategy.value,
                    "file_type": file_type,
                    "embedding_svc": self.embedding_svc,
                }
            )
            return {
                "chunks": [
                    SmartChunk(
                        id="smart-1",
                        content="NEW SMART CHUNK",
                        start_char=0,
                        end_char=15,
                        metadata=ChunkMetadata(
                            level=ChunkLevel.PARAGRAPH,
                            section_title="Intro",
                            token_count=4,
                            extra={"engine": "fake"},
                        ),
                    )
                ],
                "hierarchy": None,
            }

    class _FakePolicyService:
        async def estimate_kb_paragraph_chunks(self, _db, _kb_id):
            return 0

        def decide_dimension(self, *, corpus_chunks, embedding_model, previous_dimension):
            _ = embedding_model, previous_dimension
            return SimpleNamespace(
                target_dimension=3,
                reason="test",
                corpus_chunks=corpus_chunks,
                should_rebuild=False,
            )

    class _FakeEmbeddingSvc:
        def _get_model(self):
            return "fake/model"

        def get_dimension(self):
            return 3

    async def _noop_publish(**_kwargs):
        return None

    monkeypatch.setattr(knowledge_api.settings, "pdf_rag_line_pipeline_enabled", True)
    monkeypatch.setattr(knowledge_api.settings, "chunk_quality_gate_enabled", False)
    monkeypatch.setattr(knowledge_api.settings, "embedding_dim_rebuild_async", False)
    monkeypatch.setattr(knowledge_api, "_publish_document_status_event", _noop_publish)
    monkeypatch.setattr(knowledge_api, "get_document_processor", lambda *_args, **_kwargs: _FakeProcessor())
    monkeypatch.setattr(knowledge_api, "get_pdf_rag_ingest_service", lambda: pdf_rag_service)
    monkeypatch.setattr(knowledge_api, "SmartChunkingService", _FakeSmartChunkingService)
    monkeypatch.setattr(knowledge_api, "get_embedding_dimension_policy_service", lambda: _FakePolicyService())
    monkeypatch.setattr(knowledge_api, "get_embedding_service_for_model_and_dimension", lambda *_args, **_kwargs: _FakeEmbeddingSvc())

    import app.core.database as core_database

    monkeypatch.setattr(core_database, "async_session_factory", lambda: _FakeSessionContext(db))

    asyncio.run(knowledge_api.process_document_task(doc_id=191, chunk_size=500, chunk_overlap=50))

    assert doc.status == DocumentStatus.COMPLETED.value
    assert doc.chunk_count == 1
    assert _FakeSmartChunkingService.calls
    assert _FakeSmartChunkingService.calls[0]["text"] == "# Intro\n\nThis text should go to smart chunking."
    assert _FakeSmartChunkingService.calls[0]["strategy"] == "hybrid"
    assert _FakeSmartChunkingService.calls[0]["embedding_svc"] is _FakeProcessor.last_embedding_svc
    saved_chunks = [obj for obj in db.added if isinstance(obj, DocumentChunk)]
    assert saved_chunks
    assert saved_chunks[0].content == "NEW SMART CHUNK"
    assert "LEGACY PDF CHUNK SHOULD NOT BE USED" not in saved_chunks[0].content
    assert saved_chunks[0].metadata_["pdf_source"] == {
        "block_ids": ["h1", "p1"],
        "block_types": ["heading", "paragraph"],
        "page_start": 1,
        "page_end": 1,
        "section_paths": ["Intro"],
    }


def test_process_document_task_should_skip_duplicate_content_before_chunking(monkeypatch):
    kb = KnowledgeBase(id=93, user_id=7, name="KB", chunk_size=500, chunk_overlap=50)
    kb.metadata_ = {
        "chunking_config": {
            "strategy": "hybrid",
            "enable_hierarchical": True,
        }
    }
    doc = Document(
        id=193,
        knowledge_base_id=93,
        filename="stored.md",
        original_filename="paper.md",
        file_path="/tmp/paper.md",
        file_size=128,
        file_type="md",
        status=DocumentStatus.PENDING.value,
        metadata_={
            "ingest_request": {
                "mode": "local_fast",
                "requested_by": 7,
            }
        },
    )
    duplicate_doc = Document(
        id=88,
        knowledge_base_id=93,
        filename="existing.md",
        original_filename="existing.md",
        file_path="/tmp/existing.md",
        file_size=128,
        file_type="md",
        status=DocumentStatus.COMPLETED.value,
        content_hash="dup-hash",
    )
    db = _FakeProcessPipelineDB(doc=doc, kb=kb)

    class _FakeProcessor:
        async def extract_text(self, _file_path, _file_type):
            return "Same text\n\nwith spacing."

        async def embed_chunks(self, texts, embedding_svc=None):
            raise AssertionError("duplicate content should skip embedding")

        def compute_hash(self, text):
            return "dup-hash"

        def estimate_tokens(self, text):
            return max(1, len(str(text)) // 4)

    class _FailSmartChunkingService:
        def __init__(self, *args, **kwargs):
            _ = args, kwargs

        async def chunk_document(self, text, config, file_type):
            _ = text, config, file_type
            raise AssertionError("duplicate content should skip smart chunking")

    async def _noop_publish(**_kwargs):
        return None

    async def _fake_find_duplicate(*_args, **_kwargs):
        return duplicate_doc

    monkeypatch.setattr(knowledge_api, "_publish_document_status_event", _noop_publish)
    monkeypatch.setattr(knowledge_api, "get_document_processor", lambda *_args, **_kwargs: _FakeProcessor())
    monkeypatch.setattr(knowledge_api, "SmartChunkingService", _FailSmartChunkingService)
    monkeypatch.setattr(knowledge_api, "_find_duplicate_document_by_content_hash", _fake_find_duplicate)

    import app.core.database as core_database

    monkeypatch.setattr(core_database, "async_session_factory", lambda: _FakeSessionContext(db))

    asyncio.run(knowledge_api.process_document_task(doc_id=193, chunk_size=500, chunk_overlap=50))

    assert doc.status == DocumentStatus.COMPLETED.value
    assert doc.chunk_count == 0
    assert doc.content is None
    assert doc.processed_at is not None
    dedupe = dict((doc.metadata_ or {}).get("dedupe") or {})
    assert dedupe["duplicate_type"] == "content_exact"
    assert int(dedupe["duplicate_of_document_id"]) == 88
    assert dedupe["indexed"] is False
    assert not [obj for obj in db.added if isinstance(obj, DocumentChunk)]


def test_process_document_task_should_trim_overlong_section_title_before_saving(monkeypatch):
    kb = KnowledgeBase(id=92, user_id=7, name="KB", chunk_size=500, chunk_overlap=50)
    kb.metadata_ = {
        "chunking_config": {
            "strategy": "hybrid",
            "enable_hierarchical": True,
        }
    }
    doc = Document(
        id=192,
        knowledge_base_id=92,
        filename="stored.pdf",
        original_filename="paper.pdf",
        file_path="/tmp/paper.pdf",
        file_size=128,
        file_type="pdf",
        status=DocumentStatus.PENDING.value,
        metadata_={
            "ingest_request": {
                "mode": "local_fast",
                "requested_by": 7,
            }
        },
    )
    db = _FakeProcessPipelineDB(doc=doc, kb=kb)
    pdf_rag_service = _FakePdfRagService(
        {
            "applied": True,
            "document_text": "# Intro\n\nThis text should go to smart chunking.",
            "chunks": [],
            "report": {"mode": "fast"},
            "extractor": "local_structured_pdf_fast",
        }
    )
    long_title = "A" * 510

    class _FakeProcessor:
        last_pdf_extractor = None

        async def extract_text(self, _file_path, _file_type):
            raise AssertionError("local pdf path should reuse PdfRagIngestService document_text")

        async def embed_chunks(self, texts, embedding_svc=None):
            _ = embedding_svc
            return [[0.1, 0.2, 0.3] for _ in texts]

        def compute_hash(self, text):
            return f"hash:{len(text)}"

        def estimate_tokens(self, text):
            return max(1, len(str(text)) // 4)

    class _FakeSmartChunkingService:
        def __init__(self, *args, **kwargs):
            _ = args, kwargs

        async def chunk_document(self, text, config, file_type):
            _ = text, config, file_type
            return {
                "chunks": [
                    SmartChunk(
                        id="smart-1",
                        content="NEW SMART CHUNK",
                        start_char=0,
                        end_char=15,
                        metadata=ChunkMetadata(
                            level=ChunkLevel.PARAGRAPH,
                            section_title=long_title,
                            section_type="introduction",
                            token_count=4,
                            extra={"engine": "fake"},
                        ),
                    )
                ],
                "hierarchy": None,
            }

    class _FakePolicyService:
        async def estimate_kb_paragraph_chunks(self, _db, _kb_id):
            return 0

        def decide_dimension(self, *, corpus_chunks, embedding_model, previous_dimension):
            _ = embedding_model, previous_dimension
            return SimpleNamespace(
                target_dimension=3,
                reason="test",
                corpus_chunks=corpus_chunks,
                should_rebuild=False,
            )

    class _FakeEmbeddingSvc:
        def _get_model(self):
            return "fake/model"

        def get_dimension(self):
            return 3

    async def _noop_publish(**_kwargs):
        return None

    monkeypatch.setattr(knowledge_api.settings, "pdf_rag_line_pipeline_enabled", True)
    monkeypatch.setattr(knowledge_api.settings, "chunk_quality_gate_enabled", False)
    monkeypatch.setattr(knowledge_api.settings, "embedding_dim_rebuild_async", False)
    monkeypatch.setattr(knowledge_api, "_publish_document_status_event", _noop_publish)
    monkeypatch.setattr(knowledge_api, "get_document_processor", lambda *_args, **_kwargs: _FakeProcessor())
    monkeypatch.setattr(knowledge_api, "get_pdf_rag_ingest_service", lambda: pdf_rag_service)
    monkeypatch.setattr(knowledge_api, "SmartChunkingService", _FakeSmartChunkingService)
    monkeypatch.setattr(knowledge_api, "get_embedding_dimension_policy_service", lambda: _FakePolicyService())
    monkeypatch.setattr(knowledge_api, "get_embedding_service_for_model_and_dimension", lambda *_args, **_kwargs: _FakeEmbeddingSvc())

    import app.core.database as core_database

    monkeypatch.setattr(core_database, "async_session_factory", lambda: _FakeSessionContext(db))

    asyncio.run(knowledge_api.process_document_task(doc_id=192, chunk_size=500, chunk_overlap=50))

    assert doc.status == DocumentStatus.COMPLETED.value
    saved_chunks = [obj for obj in db.added if isinstance(obj, DocumentChunk)]
    assert len(saved_chunks) == 1
    assert saved_chunks[0].section_title == long_title[:500]
    assert len(saved_chunks[0].section_title) == 500


def test_process_document_task_should_skip_reference_paragraph_chunks(monkeypatch):
    kb = KnowledgeBase(id=94, user_id=7, name="KB", chunk_size=500, chunk_overlap=50)
    kb.metadata_ = {
        "chunking_config": {
            "strategy": "hybrid",
            "enable_hierarchical": True,
        }
    }
    doc = Document(
        id=194,
        knowledge_base_id=94,
        filename="stored.md",
        original_filename="paper.md",
        file_path="/tmp/paper.md",
        file_size=128,
        file_type="md",
        status=DocumentStatus.PENDING.value,
        metadata_={},
    )
    db = _FakeProcessPipelineDB(doc=doc, kb=kb)

    class _FakeProcessor:
        last_pdf_extractor = None

        async def extract_text(self, _file_path, _file_type):
            return "# Intro\n\nBody"

        async def embed_chunks(self, texts, embedding_svc=None):
            _ = embedding_svc
            return [[0.1, 0.2, 0.3] for _ in texts]

        def compute_hash(self, text):
            return f"hash:{len(text)}"

        def estimate_tokens(self, text):
            return max(1, len(str(text)) // 4)

    class _FakeSmartChunkingService:
        def __init__(self, *args, **kwargs):
            _ = args, kwargs

        async def chunk_document(self, text, config, file_type):
            _ = text, config, file_type
            return {
                "chunks": [
                    SmartChunk(
                        id="intro-1",
                        content="Main body chunk",
                        start_char=0,
                        end_char=15,
                        metadata=ChunkMetadata(
                            level=ChunkLevel.PARAGRAPH,
                            section_title="Introduction",
                            section_type="introduction",
                            token_count=4,
                            extra={"engine": "fake"},
                        ),
                    ),
                    SmartChunk(
                        id="ref-1",
                        content="Smith et al. 2024",
                        start_char=16,
                        end_char=34,
                        metadata=ChunkMetadata(
                            level=ChunkLevel.PARAGRAPH,
                            section_title="References",
                            section_type="references",
                            token_count=4,
                            extra={"engine": "fake"},
                        ),
                    ),
                ],
                "hierarchy": None,
            }

    class _FakePolicyService:
        async def estimate_kb_paragraph_chunks(self, _db, _kb_id):
            return 0

        def decide_dimension(self, *, corpus_chunks, embedding_model, previous_dimension):
            _ = embedding_model, previous_dimension
            return SimpleNamespace(
                target_dimension=3,
                reason="test",
                corpus_chunks=corpus_chunks,
                should_rebuild=False,
            )

    class _FakeEmbeddingSvc:
        def _get_model(self):
            return "fake/model"

        def get_dimension(self):
            return 3

    async def _noop_publish(**_kwargs):
        return None

    monkeypatch.setattr(knowledge_api.settings, "chunk_quality_gate_enabled", False)
    monkeypatch.setattr(knowledge_api.settings, "embedding_dim_rebuild_async", False)
    monkeypatch.setattr(knowledge_api, "_publish_document_status_event", _noop_publish)
    monkeypatch.setattr(knowledge_api, "get_document_processor", lambda *_args, **_kwargs: _FakeProcessor())
    monkeypatch.setattr(knowledge_api, "SmartChunkingService", _FakeSmartChunkingService)
    monkeypatch.setattr(knowledge_api, "get_embedding_dimension_policy_service", lambda: _FakePolicyService())
    monkeypatch.setattr(knowledge_api, "get_embedding_service_for_model_and_dimension", lambda *_args, **_kwargs: _FakeEmbeddingSvc())

    import app.core.database as core_database

    monkeypatch.setattr(core_database, "async_session_factory", lambda: _FakeSessionContext(db))

    asyncio.run(knowledge_api.process_document_task(doc_id=194, chunk_size=500, chunk_overlap=50))

    saved_chunks = [obj for obj in db.added if isinstance(obj, DocumentChunk)]
    assert len(saved_chunks) == 1
    assert saved_chunks[0].content == "Main body chunk"
    assert doc.chunk_count == 1
    assert (doc.metadata_ or {}).get("reference_filter", {}).get("primary_dropped") == 1


def test_filter_reference_chunks_should_use_pdf_source_section_paths():
    chunks = [
        {
            "id": "main-1",
            "content": "Main body chunk",
            "start_char": 0,
            "end_char": 10,
            "metadata": {
                "level": "paragraph",
                "extra": {
                    "pdf_source": {
                        "section_paths": ["Introduction"],
                    }
                },
            },
        },
        {
            "id": "ref-1",
            "content": "Ref chunk",
            "start_char": 10,
            "end_char": 20,
            "metadata": {
                "level": "paragraph",
                "extra": {
                    "pdf_source": {
                        "section_paths": ["Appendix > References"],
                    }
                },
            },
        },
    ]

    filtered, dropped = knowledge_api._filter_reference_chunks(chunks)

    assert dropped == 1
    assert [item["id"] for item in filtered] == ["main-1"]


def test_pdf_structural_postprocess_should_merge_heading_fragment():
    text = "# Results\n\nMain finding sentence."
    heading_end = text.index("\n\n")
    paragraph_start = heading_end + 2
    source_spans = [
        {
            "start_char": 0,
            "end_char": heading_end,
            "block_id": "h1",
            "block_type": "heading",
            "page_start": 1,
            "page_end": 1,
            "section_path": "Results",
        },
        {
            "start_char": paragraph_start,
            "end_char": len(text),
            "block_id": "p1",
            "block_type": "paragraph",
            "page_start": 1,
            "page_end": 1,
            "section_path": "Results",
        },
    ]
    chunks = [
        {
            "id": "heading-fragment",
            "content": text[:heading_end],
            "start_char": 0,
            "end_char": heading_end,
            "metadata": {
                "level": "paragraph",
                "section_title": None,
                "token_count": 2,
                "extra": {"engine": "llamaindex", "splitter": "SemanticSplitterNodeParser"},
            },
        },
        {
            "id": "body-fragment",
            "content": text[paragraph_start:],
            "start_char": paragraph_start,
            "end_char": len(text),
            "metadata": {
                "level": "paragraph",
                "section_title": None,
                "token_count": 10,
                "extra": {"engine": "llamaindex", "splitter": "SemanticSplitterNodeParser"},
            },
        },
    ]

    processed, report = knowledge_api._apply_pdf_source_structural_postprocess(
        chunks,
        text=text,
        source_spans=source_spans,
        min_tokens=32,
        max_tokens=384,
    )

    assert report["merge_count"] == 1
    assert len(processed) == 1
    assert processed[0]["start_char"] == 0
    assert processed[0]["end_char"] == len(text)
    assert processed[0]["metadata"]["section_title"] == "Results"
    assert processed[0]["metadata"]["extra"]["pdf_source"]["block_types"] == ["heading", "paragraph"]


def test_pdf_structural_postprocess_should_preserve_third_party_boundary_for_mixed_table_region():
    text = "Overview text.\n\nTable 1. Caption\n\n| A | B |\n| --- | --- |\n| 1 | 2 |"
    paragraph_end = text.index("\n\nTable")
    caption_start = paragraph_end + 2
    caption_end = text.index("\n\n| A |")
    table_start = caption_end + 2
    source_spans = [
        {
            "start_char": 0,
            "end_char": paragraph_end,
            "block_id": "p1",
            "block_type": "paragraph",
            "page_start": 1,
            "page_end": 1,
            "section_path": "Supplement",
        },
        {
            "start_char": caption_start,
            "end_char": caption_end,
            "block_id": "c1",
            "block_type": "caption",
            "page_start": 1,
            "page_end": 1,
            "section_path": "Supplement",
        },
        {
            "start_char": table_start,
            "end_char": len(text),
            "block_id": "t1",
            "block_type": "table",
            "page_start": 1,
            "page_end": 1,
            "section_path": "Supplement",
        },
    ]
    chunks = [
        {
            "id": "mixed-1",
            "content": text,
            "start_char": 0,
            "end_char": len(text),
            "metadata": {
                "level": "paragraph",
                "section_title": None,
                "token_count": 48,
                "extra": {"engine": "llamaindex", "splitter": "SemanticSplitterNodeParser"},
            },
        }
    ]

    processed, report = knowledge_api._apply_pdf_source_structural_postprocess(
        chunks,
        text=text,
        source_spans=source_spans,
        min_tokens=32,
        max_tokens=384,
    )

    assert report["split_count"] == 0
    assert report["merge_count"] == 0
    assert len(processed) == 1
    assert processed[0]["metadata"]["section_title"] == "Supplement"
    assert processed[0]["metadata"]["extra"]["pdf_source"]["block_types"] == [
        "paragraph",
        "caption",
        "table",
    ]


def test_pdf_structural_postprocess_should_merge_small_footnote_fragment():
    paragraph = ("Main finding sentence. " * 18).strip()
    footnote = "1 supplementary note"
    text = f"{paragraph}\n\n{footnote}"
    paragraph_end = len(paragraph)
    footnote_start = paragraph_end + 2

    source_spans = [
        {
            "start_char": 0,
            "end_char": paragraph_end,
            "block_id": "p1",
            "block_type": "paragraph",
            "page_start": 1,
            "page_end": 1,
            "section_path": "Results",
        },
        {
            "start_char": footnote_start,
            "end_char": len(text),
            "block_id": "f1",
            "block_type": "footnote",
            "page_start": 1,
            "page_end": 1,
            "section_path": "Results",
        },
    ]
    chunks = [
        {
            "id": "body-fragment",
            "content": paragraph,
            "start_char": 0,
            "end_char": paragraph_end,
            "metadata": {
                "level": "paragraph",
                "section_title": "Results",
                "token_count": 60,
                "extra": {"engine": "llamaindex", "splitter": "SemanticSplitterNodeParser"},
            },
        },
        {
            "id": "footnote-fragment",
            "content": footnote,
            "start_char": footnote_start,
            "end_char": len(text),
            "metadata": {
                "level": "paragraph",
                "section_title": None,
                "token_count": 3,
                "extra": {"engine": "llamaindex", "splitter": "SemanticSplitterNodeParser"},
            },
        },
    ]

    processed, report = knowledge_api._apply_pdf_source_structural_postprocess(
        chunks,
        text=text,
        source_spans=source_spans,
        min_tokens=32,
        max_tokens=384,
    )

    assert report["split_count"] == 0
    assert report["merge_count"] == 1
    assert len(processed) == 1
    assert processed[0]["metadata"]["section_title"] == "Results"
    assert processed[0]["metadata"]["extra"]["pdf_source"]["block_types"] == ["paragraph", "footnote"]


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

    scheduled: list[tuple[int, int, int]] = []

    async def _fake_schedule(doc_id: int, chunk_size: int, chunk_overlap: int) -> bool:
        scheduled.append((int(doc_id), int(chunk_size), int(chunk_overlap)))
        return True

    monkeypatch.setattr(knowledge_api, "_publish_document_status_event", _noop_publish)
    monkeypatch.setattr(knowledge_api, "_schedule_document_task", _fake_schedule)
    monkeypatch.setattr(knowledge_api.os.path, "exists", lambda path: path == "/tmp/paper.pdf")
    _reset_task_state()

    status = asyncio.run(
        knowledge_api.retry_document_processing(
            kb_id=84,
            doc_id=147,
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
    assert scheduled == [(147, 500, 50)]
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

    async def _fake_schedule(doc_id: int, chunk_size: int, chunk_overlap: int) -> bool:
        scheduled.append((int(doc_id), int(chunk_size), int(chunk_overlap)))
        return True

    monkeypatch.setattr(knowledge_api, "async_session_factory", lambda: _FakeSessionContext(db))
    monkeypatch.setattr(knowledge_api.os.path, "exists", lambda path: path == "/tmp/paper.pdf")
    monkeypatch.setattr(knowledge_api, "_schedule_document_task", _fake_schedule)
    monkeypatch.setattr(knowledge_api.settings, "knowledge_resume_running_documents_on_startup", True)
    monkeypatch.setattr(knowledge_api.settings, "knowledge_resume_running_documents_limit", 5)
    _reset_task_state()

    report = asyncio.run(knowledge_api.resume_interrupted_document_tasks_on_startup())

    assert report["enabled"] is True
    assert report["scheduled"] == 1
    assert report["marked_failed"] == 0
    assert report["documents"] == [147]
    retry_request = dict((doc.metadata_ or {}).get("retry_request") or {})
    assert retry_request["trigger"] == "startup"
    assert retry_request["reason"] == "startup_resume_from_cache"
    assert retry_request["count"] == 1
    assert doc.status == DocumentStatus.PENDING.value
    assert (doc.error_message or "") == ""
    assert db.commit_calls == 1
    assert scheduled == [(147, 700, 70)]


def test_schedule_document_task_should_queue_by_concurrency_limit(monkeypatch):
    _reset_task_state()
    monkeypatch.setattr(knowledge_api.settings, "knowledge_document_task_max_concurrency", 1)

    started: list[int] = []
    finished: list[int] = []
    release_first = asyncio.Event()
    first_started = asyncio.Event()
    second_started = asyncio.Event()

    async def _fake_process_document_task(doc_id: int, chunk_size: int, chunk_overlap: int):
        started.append(int(doc_id))
        if int(doc_id) == 1:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        finished.append(int(doc_id))

    monkeypatch.setattr(knowledge_api, "process_document_task", _fake_process_document_task)

    async def _run():
        queued_first = await knowledge_api._schedule_document_task(1, 500, 50)
        queued_second = await knowledge_api._schedule_document_task(2, 500, 50)
        assert queued_first is True
        assert queued_second is True
        await asyncio.wait_for(first_started.wait(), timeout=1)
        assert started == [1]
        assert not second_started.is_set()
        release_first.set()
        await asyncio.wait_for(second_started.wait(), timeout=1)
        await asyncio.gather(*list(knowledge_api._DOCUMENT_TASK_HANDLES.values()))

    asyncio.run(_run())

    assert started == [1, 2]
    assert finished == [1, 2]


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

    async def _fake_schedule(doc_id: int, chunk_size: int, chunk_overlap: int) -> bool:
        scheduled.append((int(doc_id), int(chunk_size), int(chunk_overlap)))
        return True

    monkeypatch.setattr(knowledge_api, "_publish_document_status_event", _noop_publish)
    monkeypatch.setattr(knowledge_api.os.path, "exists", lambda path: path == "/tmp/paper.pdf")
    monkeypatch.setattr(knowledge_api, "_schedule_document_task", _fake_schedule)
    _reset_task_state()

    status = asyncio.run(
        knowledge_api.get_document_status(
            kb_id=84,
            doc_id=147,
            db=db,
            current_user=SimpleNamespace(id=7),
        )
    )

    assert status.status == DocumentStatus.PENDING.value
    assert status.message == "排队中"
    assert status.processing_stage == "queued"
    assert status.processing_stage_label == "排队中"
    retry_request = dict((doc.metadata_ or {}).get("retry_request") or {})
    assert retry_request["trigger"] == "status_poll"
    assert retry_request["reason"] == "status_poll_resume_from_cache"
    assert retry_request["count"] == 1
    assert scheduled == [(147, 640, 64)]
    assert db.commit_calls >= 1


def test_get_document_status_should_resume_orphaned_pending_doc(monkeypatch):
    kb = KnowledgeBase(id=84, user_id=7, name="KB", chunk_size=640, chunk_overlap=64)
    doc = Document(
        id=148,
        knowledge_base_id=84,
        filename="stored.pdf",
        original_filename="paper.pdf",
        file_path="/tmp/paper.pdf",
        file_size=128,
        file_type="pdf",
        status=DocumentStatus.PENDING.value,
        metadata_={},
    )
    knowledge_api._set_document_processing_stage(doc, stage="queued")
    db = _FakeProcessDB(doc=doc, kb=kb)
    scheduled = []

    async def _noop_publish(**_kwargs):
        return None

    async def _fake_schedule(doc_id: int, chunk_size: int, chunk_overlap: int) -> bool:
        scheduled.append((int(doc_id), int(chunk_size), int(chunk_overlap)))
        return True

    monkeypatch.setattr(knowledge_api, "_publish_document_status_event", _noop_publish)
    monkeypatch.setattr(knowledge_api.os.path, "exists", lambda path: path == "/tmp/paper.pdf")
    monkeypatch.setattr(knowledge_api, "_schedule_document_task", _fake_schedule)
    _reset_task_state()

    status = asyncio.run(
        knowledge_api.get_document_status(
            kb_id=84,
            doc_id=148,
            db=db,
            current_user=SimpleNamespace(id=7),
        )
    )

    assert status.status == DocumentStatus.PENDING.value
    assert status.message == "排队中"
    assert status.processing_stage == "queued"
    retry_request = dict((doc.metadata_ or {}).get("retry_request") or {})
    assert retry_request["trigger"] == "status_poll"
    assert retry_request["reason"] == "status_poll_queue_resume_restart"
    assert retry_request["count"] == 1
    assert scheduled == [(148, 640, 64)]
    assert db.commit_calls >= 1


def test_cancel_document_processing_should_mark_document_cancelled(monkeypatch):
    kb = KnowledgeBase(id=86, user_id=7, name="KB", chunk_size=500, chunk_overlap=50)
    doc = Document(
        id=149,
        knowledge_base_id=86,
        filename="stored.pdf",
        original_filename="paper.pdf",
        file_path="/tmp/paper.pdf",
        file_size=128,
        file_type="pdf",
        status=DocumentStatus.RUNNING.value,
        metadata_={},
    )
    db = _FakeProcessDB(doc=doc, kb=kb)

    async def _noop_publish(**_kwargs):
        return None

    async def _fake_cancel(_doc_id: int, *, wait_timeout_seconds: float = 5.0) -> bool:
        return True

    monkeypatch.setattr(knowledge_api, "_publish_document_status_event", _noop_publish)
    monkeypatch.setattr(knowledge_api, "_cancel_document_task", _fake_cancel)

    status = asyncio.run(
        knowledge_api.cancel_document_processing(
            kb_id=86,
            doc_id=149,
            db=db,
            current_user=SimpleNamespace(id=7),
        )
    )

    assert status.status == DocumentStatus.CANCELLED.value
    assert status.processing_stage == "cancelled"
    assert doc.status == DocumentStatus.CANCELLED.value
    assert doc.error_message == "文档处理已取消"


def test_delete_document_should_cancel_active_task_before_delete(monkeypatch):
    kb = KnowledgeBase(id=87, user_id=7, name="KB", chunk_size=500, chunk_overlap=50)
    doc = Document(
        id=150,
        knowledge_base_id=87,
        filename="stored.pdf",
        original_filename="paper.pdf",
        file_path="/tmp/paper.pdf",
        file_size=128,
        file_type="pdf",
        status=DocumentStatus.RUNNING.value,
        metadata_={},
    )
    db = _FakeDeleteDB(doc=doc, kb=kb)
    cancelled: list[int] = []

    async def _fake_cancel(doc_id: int, *, wait_timeout_seconds: float = 5.0) -> bool:
        cancelled.append(int(doc_id))
        return True

    def _fake_has_live_task(_doc_id: int) -> bool:
        return not cancelled

    monkeypatch.setattr(knowledge_api, "_cancel_document_task", _fake_cancel)
    monkeypatch.setattr(knowledge_api, "_has_live_document_task", _fake_has_live_task)
    monkeypatch.setattr(knowledge_api.os.path, "exists", lambda _path: False)

    result = asyncio.run(
        knowledge_api.delete_document(
            kb_id=87,
            doc_id=150,
            db=db,
            current_user=SimpleNamespace(id=7),
        )
    )

    assert cancelled == [150]
    assert len(db.deleted) == 1
    assert result["message"] == "删除成功"


def test_process_document_task_should_mark_document_cancelled_when_explicitly_cancelled(monkeypatch):
    kb = KnowledgeBase(id=88, user_id=7, name="KB", chunk_size=500, chunk_overlap=50)
    doc = Document(
        id=151,
        knowledge_base_id=88,
        filename="stored.txt",
        original_filename="paper.txt",
        file_path="/tmp/paper.txt",
        file_size=128,
        file_type="txt",
        status=DocumentStatus.PENDING.value,
        metadata_={},
    )
    db = _FakeProcessDB(doc=doc, kb=kb)

    class _SlowProcessor:
        last_pdf_extractor = None

        async def extract_text(self, *_args, **_kwargs):
            await asyncio.sleep(10)
            return "text"

        def compute_hash(self, text: str) -> str:
            return text

        def estimate_tokens(self, text: str) -> int:
            return len(text)

    async def _noop_publish(**_kwargs):
        return None

    import app.core.database as core_database

    monkeypatch.setattr(core_database, "async_session_factory", lambda: _FakeSessionContext(db))
    monkeypatch.setattr(knowledge_api, "_publish_document_status_event", _noop_publish)
    monkeypatch.setattr(knowledge_api, "get_document_processor", lambda *_args, **_kwargs: _SlowProcessor())
    _reset_task_state()

    async def _run():
        task = asyncio.create_task(
            knowledge_api.process_document_task(doc_id=151, chunk_size=500, chunk_overlap=50)
        )
        await asyncio.sleep(0.05)
        await knowledge_api._mark_document_task_cancellation_requested(151)
        task.cancel()
        await task

    asyncio.run(_run())

    assert doc.status == DocumentStatus.CANCELLED.value
    assert (doc.metadata_ or {}).get("processing_state", {}).get("stage") == "cancelled"


def test_get_document_status_returns_running_stage_message():
    kb = KnowledgeBase(id=85, user_id=7, name="KB", chunk_size=640, chunk_overlap=64)
    doc = Document(
        id=148,
        knowledge_base_id=85,
        filename="stored.pdf",
        original_filename="paper.pdf",
        file_path="/tmp/paper.pdf",
        file_size=128,
        file_type="pdf",
        status=DocumentStatus.RUNNING.value,
        metadata_={
            "processing_state": {
                "stage": "embedding",
                "stage_label": "向量化中",
                "progress": 78,
                "detail": "共 42 个分块",
            }
        },
    )
    db = _FakeProcessDB(doc=doc, kb=kb)

    status = asyncio.run(
        knowledge_api.get_document_status(
            kb_id=85,
            doc_id=148,
            db=db,
            current_user=SimpleNamespace(id=7),
        )
    )

    assert status.status == DocumentStatus.RUNNING.value
    assert status.progress == 78
    assert status.message == "向量化中"
    assert status.processing_stage == "embedding"
    assert status.processing_stage_label == "向量化中"
    assert status.processing_detail == "共 42 个分块"


def test_collect_status_stream_snapshot_returns_document_status_payloads():
    docs = [
        Document(
            id=163,
            knowledge_base_id=146,
            filename="stored.pdf",
            original_filename="2505.02390v2.pdf",
            file_path="/tmp/2505.02390v2.pdf",
            file_size=128,
            file_type="pdf",
            status=DocumentStatus.RUNNING.value,
            chunk_count=12,
            metadata_={
                "processing_state": {
                    "stage": "embedding",
                    "stage_label": "向量化中",
                    "progress": 82,
                    "detail": "共 79 个分块",
                    "updated_at": "2026-03-29T14:40:06",
                }
            },
        ),
        Document(
            id=162,
            knowledge_base_id=146,
            filename="stored-2.pdf",
            original_filename="another.pdf",
            file_path="/tmp/another.pdf",
            file_size=64,
            file_type="pdf",
            status=DocumentStatus.COMPLETED.value,
            chunk_count=34,
            metadata_={
                "processing_state": {
                    "stage": "completed",
                    "stage_label": "已完成",
                    "progress": 100,
                    "updated_at": "2026-03-29T14:44:11",
                }
            },
        ),
    ]
    db = _FakeSnapshotDB(docs=docs)

    payloads = asyncio.run(
        knowledge_api._collect_status_stream_snapshot(
            db,
            user_id=7,
            kb_id=146,
        )
    )

    assert [item["document_id"] for item in payloads] == [163, 162]
    assert payloads[0]["kb_id"] == 146
    assert payloads[0]["status"] == DocumentStatus.RUNNING.value
    assert payloads[0]["processing_stage"] == "embedding"
    assert payloads[0]["processing_stage_label"] == "向量化中"
    assert payloads[0]["processing_progress"] == 82
    assert payloads[0]["processing_detail"] == "共 79 个分块"
    assert payloads[0]["chunk_count"] == 12
