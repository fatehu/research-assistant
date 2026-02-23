"""Guard helpers for stale document processing states."""

from __future__ import annotations

from datetime import datetime, timezone

from app.models.knowledge import DocumentStatus


def _to_utc_naive(dt: datetime) -> datetime:
    """Normalize any datetime to UTC-naive for safe arithmetic."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def is_stale_processing_status(
    *,
    status: str,
    last_updated_at: datetime | None,
    timeout_seconds: int,
    now: datetime | None = None,
) -> bool:
    """Return True when pending/running has exceeded timeout."""
    if timeout_seconds <= 0:
        return False
    normalized = str(status or "").strip().lower()
    if normalized not in {
        DocumentStatus.PENDING.value,
        DocumentStatus.RUNNING.value,
        "processing",  # 兼容历史数据
    }:
        return False
    if last_updated_at is None:
        return False

    current = _to_utc_naive(now or datetime.utcnow())
    updated = _to_utc_naive(last_updated_at)
    age_seconds = (current - updated).total_seconds()
    return age_seconds >= timeout_seconds


def build_timeout_error_message(timeout_seconds: int) -> str:
    timeout_minutes = max(timeout_seconds // 60, 1)
    return (
        f"文档处理超时（超过 {timeout_minutes} 分钟），"
        "请删除后重新上传，或稍后重试。"
    )
