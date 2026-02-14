import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.codelab_executor import CodeLabExecutor


def test_codelab_executor_enforces_hard_timeout():
    executor = CodeLabExecutor(notebook_id="test-timeout", hard_timeout_seconds=1)
    try:
        result = executor.execute("while True:\n    pass", timeout_seconds=1)
        assert result["success"] is False
        assert result["terminated_reason"] == "timeout"
        assert result["policy_violation_code"] is None
    finally:
        executor.close()

