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

