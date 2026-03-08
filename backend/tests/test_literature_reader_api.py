import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api import literature as literature_api
from app.models.knowledge import DocumentStatus
from app.models.literature import KnowledgeLinkStatus


class _FakeResult:
    def __init__(self, *, row=None, rows=None):
        self._row = row
        self._rows = list(rows or [])

    def scalar_one_or_none(self):
        return self._row

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, _query):
        if not self._results:
            return _FakeResult(rows=[])
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_stream_paper_pdf_reads_local_file(monkeypatch, tmp_path: Path):
    pdf_path = tmp_path / "paper_10.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%EOF\n")

    paper = SimpleNamespace(id=10, title="Test Paper", pdf_path=str(pdf_path), pdf_url="https://example.com/a.pdf")

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)

    response = await literature_api.stream_paper_pdf(
        paper_id=10,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=99),
    )

    assert isinstance(response, FileResponse)
    assert Path(response.path) == pdf_path
    assert response.headers.get("content-disposition", "").startswith("inline;")


@pytest.mark.asyncio
async def test_stream_paper_pdf_raises_404_when_missing(monkeypatch):
    paper = SimpleNamespace(id=11, title="Missing Paper", pdf_path=None, pdf_url="https://example.com/missing.pdf")

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)

    with pytest.raises(HTTPException) as exc:
        await literature_api.stream_paper_pdf(
            paper_id=11,
            db=SimpleNamespace(),
            current_user=SimpleNamespace(id=99),
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_create_reader_composed_review_session_forwards_cache_clone_flags(monkeypatch):
    paper = SimpleNamespace(id=78, title="Demo Paper", pdf_path="demo.pdf")
    captured: dict[str, object] = {}

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeService:
        async def create_review_session(self, **kwargs):
            captured.update(kwargs)
            return {
                "snapshot_id": "snapshot_fast",
                "session_id": "session_fast",
                "page": 7,
                "paper_id": 78,
                "source_signature": "sig-fast",
                "ui_plan": {"components": [], "layout": {}, "style_tokens": {}, "trace_meta": {}},
                "assets": [],
            }

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeService())

    payload = literature_api.ReaderComposeReviewSessionRequest(
        page=7,
        selected_kb_id=84,
        snapshot_label="snapshot_fast",
        prefer_cache_clone=True,
        allow_recompute_on_cache_miss=False,
    )

    response = await literature_api.create_reader_composed_review_session(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert response["snapshot_id"] == "snapshot_fast"
    assert captured["paper"] is paper
    assert captured["page"] == 7
    assert captured["prefer_cache_clone"] is True
    assert captured["allow_recompute_on_cache_miss"] is False


@pytest.mark.asyncio
async def test_create_reader_composed_review_session_returns_404_for_cache_miss(monkeypatch):
    paper = SimpleNamespace(id=78, title="Demo Paper", pdf_path="demo.pdf")

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeService:
        async def create_review_session(self, **_kwargs):
            raise ValueError("review_cache_not_found")

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeService())

    payload = literature_api.ReaderComposeReviewSessionRequest(
        page=7,
        prefer_cache_clone=True,
        allow_recompute_on_cache_miss=False,
    )

    with pytest.raises(HTTPException) as exc:
        await literature_api.create_reader_composed_review_session(
            paper_id=78,
            payload=payload,
            db=SimpleNamespace(),
            current_user=SimpleNamespace(id=5),
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "review_cache_not_found"


@pytest.mark.asyncio
async def test_list_literature_ask_sessions_returns_user_scoped_rows():
    now = datetime.now(timezone.utc)
    rows = [
        SimpleNamespace(
            id=1,
            user_id=7,
            scope="paper",
            paper_id=100,
            collection_id=None,
            knowledge_base_id=3,
            title="会话1",
            created_at=now,
            updated_at=now,
        )
    ]
    db = _FakeDB([_FakeResult(rows=rows)])

    result = await literature_api.list_literature_ask_sessions(
        scope=None,
        paper_id=None,
        collection_id=None,
        knowledge_base_id=None,
        limit=30,
        offset=0,
        db=db,
        current_user=SimpleNamespace(id=7),
    )

    assert len(result) == 1
    assert result[0].id == 1
    assert result[0].scope == "paper"


@pytest.mark.asyncio
async def test_list_literature_ask_messages_filters_invalid_sources():
    now = datetime.now(timezone.utc)
    session = SimpleNamespace(id=8, user_id=7)
    rows = [
        SimpleNamespace(
            id=11,
            session_id=8,
            role="assistant",
            content="answer",
            sources=[
                "bad-source",
                {
                    "document_id": 1,
                    "document_name": "paper.pdf",
                    "snippet": "snippet",
                    "score": 0.91,
                },
            ],
            created_at=now,
        )
    ]
    db = _FakeDB([_FakeResult(row=session), _FakeResult(rows=rows)])

    result = await literature_api.list_literature_ask_messages(
        session_id=8,
        limit=200,
        offset=0,
        db=db,
        current_user=SimpleNamespace(id=7),
    )

    assert len(result) == 1
    assert result[0].role == "assistant"
    assert result[0].sources[0].document_name == "paper.pdf"


@pytest.mark.asyncio
async def test_list_literature_ask_messages_raises_404_for_unknown_session():
    db = _FakeDB([_FakeResult(row=None)])

    with pytest.raises(HTTPException) as exc:
        await literature_api.list_literature_ask_messages(
            session_id=999,
            limit=200,
            offset=0,
            db=db,
            current_user=SimpleNamespace(id=7),
        )

    assert exc.value.status_code == 404


def test_derive_link_status_from_document_completed():
    doc = SimpleNamespace(id=123, status=DocumentStatus.COMPLETED.value, error_message=None)
    status, error_message, doc_id = literature_api._derive_link_status_from_document(doc)
    assert status == KnowledgeLinkStatus.COMPLETED.value
    assert error_message is None
    assert doc_id == 123


def test_derive_link_status_from_document_processing_clears_error():
    doc = SimpleNamespace(id=9, status=DocumentStatus.RUNNING.value, error_message="old error")
    status, error_message, doc_id = literature_api._derive_link_status_from_document(doc)
    assert status == KnowledgeLinkStatus.RUNNING.value
    assert error_message is None
    assert doc_id == 9


def test_mark_stale_document_timeout_marks_processing_doc_as_timeout(monkeypatch):
    monkeypatch.setattr(literature_api.settings, "document_processing_stale_timeout_seconds", 60)
    doc = SimpleNamespace(
        id=130,
        status=DocumentStatus.RUNNING.value,
        error_message=None,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        updated_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    changed = literature_api._mark_stale_document_timeout(doc)

    assert changed is True
    assert doc.status == DocumentStatus.TIMEOUT.value
    assert "文档处理超时" in doc.error_message


def test_normalize_collection_name_repairs_known_mojibake_tokens():
    for token in literature_api._build_mojibake_variants("所有论文"):
        assert literature_api._normalize_collection_name(token) == "所有论文"
    for token in literature_api._build_mojibake_variants("待读"):
        assert literature_api._normalize_collection_name(token) == "待读"
    for token in literature_api._build_mojibake_variants("已读"):
        assert literature_api._normalize_collection_name(token) == "已读"
    for token in literature_api._build_mojibake_variants("收藏"):
        assert literature_api._normalize_collection_name(token) == "收藏"
    assert literature_api._normalize_collection_name("我的收藏") == "我的收藏"


def test_normalize_collection_description_repairs_known_mojibake_tokens():
    for token in literature_api._build_mojibake_variants("所有保存的论文"):
        assert literature_api._normalize_collection_description(token) == "所有保存的论文"
    for token in literature_api._build_mojibake_variants("待阅读的论文"):
        assert literature_api._normalize_collection_description(token) == "待阅读的论文"
    for token in literature_api._build_mojibake_variants("已阅读的论文"):
        assert literature_api._normalize_collection_description(token) == "已阅读的论文"
    for token in literature_api._build_mojibake_variants("重要论文"):
        assert literature_api._normalize_collection_description(token) == "重要论文"
    assert literature_api._normalize_collection_description("用户自定义描述") == "用户自定义描述"
