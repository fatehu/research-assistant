from __future__ import annotations

from typing import Any, Dict, Optional


def build_tool_error_contract(
    *,
    code: str,
    message: str,
    tool_name: Optional[str] = None,
    stage: Optional[str] = None,
    detail: Optional[str] = None,
    retryable: bool = False,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "code": str(code),
        "message": str(message),
        "tool": (str(tool_name) if tool_name else None),
        "stage": (str(stage) if stage else None),
        "detail": (str(detail) if detail else None),
        "retryable": bool(retryable),
    }
    if isinstance(metadata, dict) and metadata:
        payload["metadata"] = metadata
    return payload


def merge_error_contract(
    data: Optional[Dict[str, Any]],
    contract: Dict[str, Any],
) -> Dict[str, Any]:
    merged = dict(data or {})
    merged["error_contract"] = dict(contract or {})
    return merged
