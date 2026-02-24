"""
Shared application error models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class AppServiceError(Exception):
    code: str
    message: str
    status_code: int = 500
    details: Optional[Any] = None
    request_id: Optional[str] = None
    # Deprecated: keep for backward compatibility while migrating callers.
    extra: Optional[Dict[str, Any]] = field(default_factory=dict)

    def to_detail(self, default_request_id: Optional[str] = None) -> Dict[str, Any]:
        payload_details = self.details if self.details is not None else (self.extra or None)
        return {
            "code": self.code,
            "message": self.message,
            "details": payload_details,
            "request_id": self.request_id or default_request_id,
        }

