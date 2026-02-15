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
    extra: Optional[Dict[str, Any]] = field(default_factory=dict)

    def to_detail(self) -> Dict[str, Any]:
        payload = {"code": self.code, "message": self.message}
        if self.extra:
            payload["extra"] = self.extra
        return payload

