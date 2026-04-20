import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.codelab_executor import CodeLabExecutor, RunnerUnavailableError


def test_codelab_executor_raises_when_runner_unavailable(monkeypatch):
    monkeypatch.setattr("app.config.settings.codelab_runner_enabled", True)
    monkeypatch.setattr("app.config.settings.codelab_runner_url", "http://127.0.0.1:9")
    monkeypatch.setattr("app.config.settings.codelab_runner_token", "test-token")

    executor = CodeLabExecutor(notebook_id="runner-down", hard_timeout_seconds=3)
    with pytest.raises(RunnerUnavailableError):
        executor.execute("1+1", timeout_seconds=2)


def test_codelab_executor_disables_runner_read_timeout_for_background_execution(monkeypatch):
    monkeypatch.setattr("app.config.settings.codelab_runner_enabled", True)
    monkeypatch.setattr("app.config.settings.codelab_runner_url", "http://runner.test")
    monkeypatch.setattr("app.config.settings.codelab_runner_token", "test-token")
    monkeypatch.setattr("app.config.settings.codelab_runner_timeout_seconds", 25)
    monkeypatch.setattr("app.config.settings.codelab_runner_connect_timeout_seconds", 3)

    captured = {}

    class _Response:
        status_code = 200
        text = "{}"

        @staticmethod
        def json():
            return {
                "success": True,
                "outputs": [],
                "execution_count": 1,
                "execution_time_ms": 1,
            }

    class _Client:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def request(self, *, method, url, headers, json):
            captured["json"] = json
            return _Response()

    monkeypatch.setattr("app.services.codelab_executor.httpx.Client", _Client)

    executor = CodeLabExecutor(notebook_id="runner-background", hard_timeout_seconds=20)
    result = executor.execute("print('long job')", timeout_seconds=0)

    assert result["success"] is True
    assert captured["json"]["timeout_seconds"] == 0
    assert captured["json"]["hard_timeout_seconds"] == 0
    assert captured["timeout"].connect == 3
    assert captured["timeout"].read is None
    assert captured["timeout"].write is None
    assert captured["timeout"].pool is None
