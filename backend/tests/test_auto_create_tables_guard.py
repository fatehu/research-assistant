import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import main as main_module


def test_auto_create_tables_disabled_does_not_call_create_tables(monkeypatch):
    called = {"value": False}

    async def _fake_create_tables():
        called["value"] = True

    monkeypatch.setattr(main_module, "create_tables", _fake_create_tables)
    monkeypatch.setattr(main_module.settings, "auto_create_tables", False)

    with TestClient(main_module.app) as client:
        response = client.get("/")
        assert response.status_code == 200

    assert called["value"] is False

