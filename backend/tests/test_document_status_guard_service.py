from datetime import datetime, timedelta

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.models.knowledge import DocumentStatus
from app.services.document_status_guard_service import (
    build_timeout_error_message,
    is_stale_processing_status,
)


def test_stale_processing_detected():
    now = datetime(2026, 2, 13, 18, 0, 0)
    updated_at = now - timedelta(hours=3)
    assert is_stale_processing_status(
        status=DocumentStatus.PROCESSING.value,
        last_updated_at=updated_at,
        timeout_seconds=7200,
        now=now,
    )


def test_recent_processing_not_stale():
    now = datetime(2026, 2, 13, 18, 0, 0)
    updated_at = now - timedelta(minutes=20)
    assert not is_stale_processing_status(
        status=DocumentStatus.PROCESSING.value,
        last_updated_at=updated_at,
        timeout_seconds=7200,
        now=now,
    )


def test_non_processing_status_never_stale():
    now = datetime(2026, 2, 13, 18, 0, 0)
    updated_at = now - timedelta(hours=10)
    assert not is_stale_processing_status(
        status=DocumentStatus.COMPLETED.value,
        last_updated_at=updated_at,
        timeout_seconds=7200,
        now=now,
    )


def test_timeout_message_contains_minutes():
    msg = build_timeout_error_message(7200)
    assert "120" in msg
    assert "超时" in msg
