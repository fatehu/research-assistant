import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import app


def test_cors_preflight_allows_whitelisted_origin():
    client = TestClient(app)
    response = client.options(
        "/api/v1/chat/send",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,authorization",
        },
    )
    assert response.status_code in (200, 204)
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_cors_preflight_rejects_non_whitelisted_origin():
    client = TestClient(app)
    response = client.options(
        "/api/v1/chat/send",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,authorization",
        },
    )
    assert response.status_code in (400, 403)
    assert response.headers.get("access-control-allow-origin") is None

