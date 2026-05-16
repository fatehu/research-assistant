import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.sandbox_runner.local_executor import _WORKER_CODE
from app.sandbox_runner.local_executor import validate_code_policy
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


def test_codelab_executor_allows_time_module():
    assert validate_code_policy("import time\nprint(round(time.time()) > 0)") is None


def test_codelab_executor_allows_warnings_module():
    assert validate_code_policy(
        "import warnings\nwarnings.filterwarnings('ignore')\nprint('ok')"
    ) is None


def test_codelab_executor_allows_joblib_module():
    assert validate_code_policy("import joblib\nprint(joblib.__version__)") is None


def test_codelab_executor_allows_common_ml_modules():
    assert validate_code_policy("import scipy\nfrom scipy import stats\nprint(stats.norm.mean())") is None
    assert validate_code_policy("import statsmodels\nprint(statsmodels.__version__)") is None
    assert validate_code_policy("import xgboost\nprint(xgboost.__version__)") is None


def test_codelab_executor_allows_common_python_builtins():
    assert '"__build_class__"' in _WORKER_CODE
    assert '"format"' in _WORKER_CODE
    assert '"getattr"' in _WORKER_CODE
    assert '"hasattr"' in _WORKER_CODE
    assert '"object"' in _WORKER_CODE
    assert '"super"' in _WORKER_CODE
    assert '"type"' in _WORKER_CODE


def test_codelab_executor_supports_class_definitions():
    executor = CodeLabExecutor(notebook_id="test-class-definition", hard_timeout_seconds=20)
    try:
        result = executor.execute(
            "class Demo(object):\n"
            "    def __init__(self, value):\n"
            "        self.value = value\n"
            "\n"
            "    def render(self):\n"
            "        return f'value={self.value}'\n"
            "\n"
            "item = Demo(3)\n"
            "print(item.render())\n",
            timeout_seconds=15,
        )
        assert result["success"] is True
        assert any("value=3" in (output.get("content") or "") for output in result["outputs"])
    finally:
        executor.close()
