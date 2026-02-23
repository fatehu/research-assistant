from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "checks" / "check_contract_alignment.py"
MODULE_SPEC = spec_from_file_location("check_contract_alignment", MODULE_PATH)
assert MODULE_SPEC and MODULE_SPEC.loader
check_contract_alignment = module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(check_contract_alignment)


def test_extract_task_status_members():
    api_text = """
export type TaskStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'timeout'
  | 'cancelled'

const x = 1
"""
    members = check_contract_alignment.extract_task_status_members(api_text)
    assert members == {
        "pending",
        "running",
        "completed",
        "failed",
        "timeout",
        "cancelled",
    }


def test_extract_normalize_tokens_and_returns():
    api_text = """
export const normalizeTaskStatus = (
  status: string | undefined | null,
): TaskStatus => {
  const normalized = String(status || '').trim().toLowerCase()
  if (normalized === 'processing' || normalized === 'running') return 'running'
  if (normalized === 'ready' || normalized === 'success' || normalized === 'done') return 'completed'
  if (normalized === 'timeout') return 'timeout'
  if (normalized === 'cancelled' || normalized === 'canceled') return 'cancelled'
  if (normalized === 'failed' || normalized === 'error') return 'failed'
  if (normalized === 'pending' || normalized === 'queued') return 'pending'
  return 'failed'
}
"""
    tokens = check_contract_alignment.extract_normalize_task_status_tokens(api_text)
    returns = check_contract_alignment.extract_normalize_task_status_returns(api_text)
    assert {"processing", "ready", "queued", "canceled"}.issubset(tokens)
    assert returns == {"running", "completed", "timeout", "cancelled", "failed", "pending"}


def test_extract_compose_and_env_defaults():
    compose_text = """
services:
  frontend:
    environment:
      - VITE_API_BASE_URL=${VITE_API_BASE_URL:-http://localhost:8888}
      - VITE_WS_BASE_URL=${VITE_WS_BASE_URL:-ws://localhost:8888}
"""
    env_text = """
VITE_API_BASE_URL=http://localhost:8888
VITE_WS_BASE_URL=ws://localhost:8888
"""
    assert (
        check_contract_alignment.extract_compose_vite_default(compose_text, "VITE_API_BASE_URL")
        == "http://localhost:8888"
    )
    assert (
        check_contract_alignment.extract_compose_vite_default(compose_text, "VITE_WS_BASE_URL")
        == "ws://localhost:8888"
    )
    assert (
        check_contract_alignment.extract_env_example_value(env_text, "VITE_API_BASE_URL")
        == "http://localhost:8888"
    )
    assert (
        check_contract_alignment.extract_env_example_value(env_text, "VITE_WS_BASE_URL")
        == "ws://localhost:8888"
    )


def test_extract_enum_members_from_class():
    model_text = """
import enum

class DocumentStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
"""
    members = check_contract_alignment.extract_enum_members_from_class(model_text, "DocumentStatus")
    assert members == {"pending", "processing", "completed", "failed"}


def test_extract_case_targets():
    migration_text = """
UPDATE documents
SET status = CASE
    WHEN lower(status) = 'running' THEN 'processing'
    WHEN lower(status) IN ('ready', 'success', 'done') THEN 'completed'
    ELSE 'failed'
END
WHERE status IS NOT NULL;
"""
    targets = check_contract_alignment.extract_case_targets(migration_text, "documents")
    assert targets == {"processing", "completed", "failed"}
