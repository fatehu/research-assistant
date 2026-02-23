import os
import sys
from datetime import datetime, timezone
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
    assert status == KnowledgeLinkStatus.READY.value
    assert error_message is None
    assert doc_id == 123


def test_derive_link_status_from_document_processing_clears_error():
    doc = SimpleNamespace(id=9, status=DocumentStatus.PROCESSING.value, error_message="old error")
    status, error_message, doc_id = literature_api._derive_link_status_from_document(doc)
    assert status == KnowledgeLinkStatus.PROCESSING.value
    assert error_message is None
    assert doc_id == 9
