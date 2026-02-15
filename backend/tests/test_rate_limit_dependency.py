import os
import sys

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.core.rate_limit import build_rate_limit_dependency


def test_rate_limit_returns_429_and_headers(monkeypatch):
    monkeypatch.setattr(settings, "api_rate_limit_enabled", True)
    monkeypatch.setattr(settings, "api_rate_limit_storage", "memory")
    monkeypatch.setattr(settings, "api_rate_limit_window_seconds", 60)

    app = FastAPI()
    dep = build_rate_limit_dependency(bucket="test", limit=2, scope="ip")

    @app.get("/limited", dependencies=[Depends(dep)])
    async def limited():
        return {"ok": True}

    client = TestClient(app)
    r1 = client.get("/limited")
    r2 = client.get("/limited")
    r3 = client.get("/limited")

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429
    assert "X-RateLimit-Limit" in r3.headers
    assert "X-RateLimit-Remaining" in r3.headers
    assert "Retry-After" in r3.headers
    detail = r3.json().get("detail", {})
    assert detail.get("code") == "rate_limited"

