import os
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api.codelab import ExecuteRequest, execute_code_directly
from app.config import settings


@pytest.fixture(autouse=True)
def _disable_runner(monkeypatch):
    monkeypatch.setattr(settings, "codelab_runner_enabled", False)


@pytest.mark.asyncio
async def test_direct_execute_rejected_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "codelab_direct_execute_enabled", False)
    with pytest.raises(HTTPException) as exc:
        await execute_code_directly(
            request=ExecuteRequest(code="1+1"),
            current_user=SimpleNamespace(id=1, role="admin"),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_direct_execute_rejected_for_non_admin_when_not_debug(monkeypatch):
    monkeypatch.setattr(settings, "codelab_direct_execute_enabled", True)
    monkeypatch.setattr(settings, "debug", False)
    with pytest.raises(HTTPException) as exc:
        await execute_code_directly(
            request=ExecuteRequest(code="1+1"),
            current_user=SimpleNamespace(id=2, role="student"),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_direct_execute_allowed_for_admin(monkeypatch):
    monkeypatch.setattr(settings, "codelab_direct_execute_enabled", True)
    monkeypatch.setattr(settings, "debug", False)
    result = await execute_code_directly(
        request=ExecuteRequest(code="1+1"),
        current_user=SimpleNamespace(id=3, role="admin"),
    )
    assert result.success is True
    assert result.terminated_reason == "none"

