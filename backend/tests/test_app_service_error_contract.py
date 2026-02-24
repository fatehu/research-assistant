from __future__ import annotations

from app.core.errors import AppServiceError


def test_app_service_error_prefers_details_and_keeps_request_id():
    err = AppServiceError(
        code="demo_error",
        message="demo message",
        details={"reason": "disk_full"},
        request_id="req-123",
        extra={"legacy": True},
    )

    payload = err.to_detail()
    assert payload["code"] == "demo_error"
    assert payload["message"] == "demo message"
    assert payload["details"] == {"reason": "disk_full"}
    assert payload["request_id"] == "req-123"


def test_app_service_error_falls_back_to_extra_and_default_request_id():
    err = AppServiceError(
        code="legacy_error",
        message="legacy message",
        extra={"legacy": "value"},
    )

    payload = err.to_detail(default_request_id="req-fallback")
    assert payload["code"] == "legacy_error"
    assert payload["message"] == "legacy message"
    assert payload["details"] == {"legacy": "value"}
    assert payload["request_id"] == "req-fallback"

