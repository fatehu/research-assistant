from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api import knowledge as knowledge_api
from app.api.knowledge import _build_error_detail, _safe_remove_file


def test_build_error_detail_shape():
    payload = _build_error_detail(
        code="file_save_failed",
        message="文件保存失败",
        details={"reason": "disk"},
        request_id="req-123",
    )
    assert payload["code"] == "file_save_failed"
    assert payload["message"] == "文件保存失败"
    assert payload["details"] == {"reason": "disk"}
    assert payload["request_id"] == "req-123"


def test_safe_remove_file_handles_missing_path(tmp_path):
    target = tmp_path / "missing.txt"
    _safe_remove_file(str(target), context="unit_test")
    assert not target.exists()


def test_safe_remove_file_deletes_existing_file(tmp_path):
    target = tmp_path / "exists.txt"
    target.write_text("ok", encoding="utf-8")
    assert os.path.exists(target)
    _safe_remove_file(str(target), context="unit_test")
    assert not os.path.exists(target)


class _ScalarSequenceDB:
    def __init__(self, values):
        self._values = list(values)

    async def scalar(self, _query):
        return self._values.pop(0)


@pytest.mark.asyncio
async def test_document_file_has_other_references_returns_true_for_shared_document():
    from app.api.knowledge import _document_file_has_other_references

    db = _ScalarSequenceDB([1])
    doc = SimpleNamespace(id=10, file_path="./uploads/shared.pdf")

    assert await _document_file_has_other_references(db, doc) is True


@pytest.mark.asyncio
async def test_document_file_has_other_references_returns_true_for_linked_paper():
    from app.api.knowledge import _document_file_has_other_references

    db = _ScalarSequenceDB([0, 1])
    doc = SimpleNamespace(id=10, file_path="./uploads/shared.pdf")

    assert await _document_file_has_other_references(db, doc) is True


@pytest.mark.asyncio
async def test_document_file_has_other_references_returns_false_without_other_links():
    from app.api.knowledge import _document_file_has_other_references

    db = _ScalarSequenceDB([0, 0])
    doc = SimpleNamespace(id=10, file_path="./uploads/solo.pdf")

    assert await _document_file_has_other_references(db, doc) is False


@pytest.mark.asyncio
async def test_delete_knowledge_base_should_preserve_linked_paper_pdf(tmp_path, monkeypatch):
    shared_pdf = tmp_path / "shared.pdf"
    shared_pdf.write_bytes(b"%PDF-1.4\n%EOF\n")
    kb = SimpleNamespace(id=41, user_id=7)
    doc = SimpleNamespace(id=12, knowledge_base_id=41, file_path=str(shared_pdf))
    removed_paths: list[str] = []

    class _FakeDeleteKnowledgeBaseDB:
        def __init__(self):
            self.deleted: list[object] = []
            self.committed = False
            self._scalar_values = [0, 1]

        async def get(self, model, ident):
            if model is knowledge_api.KnowledgeBase and int(ident) == 41:
                return kb
            return None

        async def execute(self, _query):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [doc]))

        async def scalar(self, _query):
            return self._scalar_values.pop(0)

        async def delete(self, obj):
            self.deleted.append(obj)

        async def commit(self):
            self.committed = True

    def _fake_remove(path: str | None, *, context: str):
        if path:
            removed_paths.append(path)

    monkeypatch.setattr(knowledge_api, "_safe_remove_file", _fake_remove)

    db = _FakeDeleteKnowledgeBaseDB()
    response = await knowledge_api.delete_knowledge_base(
        kb_id=41,
        db=db,
        current_user=SimpleNamespace(id=7),
    )

    assert response["message"] == "删除成功"
    assert removed_paths == []
    assert db.deleted == [kb]
    assert db.committed is True
