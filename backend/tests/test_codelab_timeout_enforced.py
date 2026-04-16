import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.codelab_executor import CodeLabExecutor


@pytest.fixture(autouse=True)
def _disable_runner(monkeypatch):
    monkeypatch.setattr("app.config.settings.codelab_runner_enabled", False)


def test_codelab_executor_enforces_hard_timeout():
    executor = CodeLabExecutor(notebook_id="test-timeout", hard_timeout_seconds=1)
    try:
        result = executor.execute("while True:\n    pass", timeout_seconds=1)
        assert result["success"] is False
        assert result["terminated_reason"] == "timeout"
        assert result["policy_violation_code"] is None
    finally:
        executor.close()


def test_codelab_executor_preserves_last_line_inside_except_block():
    executor = CodeLabExecutor(notebook_id="test-control-flow", hard_timeout_seconds=3)
    try:
        result = executor.execute(
            "try:\n"
            "    missing_helper()\n"
            "except NameError as e:\n"
            "    print('fallback reached')",
            timeout_seconds=2,
        )
        assert result["success"] is True
        assert result["terminated_reason"] == "none"
        assert any(
            output.get("output_type") == "stream" and "fallback reached" in str(output.get("content") or "")
            for output in list(result.get("outputs") or [])
        )
    finally:
        executor.close()
