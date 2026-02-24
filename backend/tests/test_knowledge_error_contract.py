from __future__ import annotations

import os

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
