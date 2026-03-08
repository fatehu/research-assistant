from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

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
