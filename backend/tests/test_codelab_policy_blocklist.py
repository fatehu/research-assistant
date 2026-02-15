import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.codelab_executor import CodeLabExecutor
@pytest.fixture(autouse=True)
def _disable_runner(monkeypatch):
    monkeypatch.setattr("app.config.settings.codelab_runner_enabled", False)


def test_codelab_executor_blocks_forbidden_import():
    executor = CodeLabExecutor(notebook_id="test-policy-import", hard_timeout_seconds=3)
    try:
        result = executor.execute("import os\nprint(os.getcwd())", timeout_seconds=2)
        assert result["success"] is False
        assert result["terminated_reason"] == "policy_violation"
        assert result["policy_violation_code"] == "forbidden_import"
    finally:
        executor.close()


def test_codelab_executor_blocks_forbidden_call():
    executor = CodeLabExecutor(notebook_id="test-policy-call", hard_timeout_seconds=3)
    try:
        result = executor.execute("__import__('os').system('echo hacked')", timeout_seconds=2)
        assert result["success"] is False
        assert result["terminated_reason"] == "policy_violation"
        assert result["policy_violation_code"] == "forbidden_call"
    finally:
        executor.close()

