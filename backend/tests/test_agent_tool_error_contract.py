from __future__ import annotations

from app.services.agent_tool_error_contract import (
    build_tool_error_contract,
    merge_error_contract,
)


def test_build_tool_error_contract_fields():
    contract = build_tool_error_contract(
        code="tool_not_found",
        message="未找到工具",
        tool_name="web_search",
        stage="dispatch",
        detail="name mismatch",
        retryable=False,
        metadata={"available": ["knowledge_search"]},
    )
    assert contract["code"] == "tool_not_found"
    assert contract["message"] == "未找到工具"
    assert contract["tool"] == "web_search"
    assert contract["stage"] == "dispatch"
    assert contract["detail"] == "name mismatch"
    assert contract["retryable"] is False
    assert contract["metadata"] == {"available": ["knowledge_search"]}


def test_merge_error_contract_keeps_existing_data():
    merged = merge_error_contract({"retry_attempt": 2}, {"code": "timeout", "message": "超时"})
    assert merged["retry_attempt"] == 2
    assert merged["error_contract"]["code"] == "timeout"
