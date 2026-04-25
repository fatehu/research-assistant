import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.agent_runtime_service import AgentRuntimeService
import app.services.agent_runtime_service as runtime_module


@pytest.mark.asyncio
async def test_append_steps_persists_text_from_event_data(monkeypatch):
    captured = []

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def add(self, record):
            captured.append(record)

        async def commit(self):
            return None

    monkeypatch.setattr(runtime_module, "async_session_factory", lambda: _FakeSession())

    service = AgentRuntimeService()
    await service.append_steps(
        "run-1",
        [
            {"type": "thought", "data": "先整理思路"},
            {"type": "answer", "data": "最终答案"},
            {
                "type": "observation",
                "data": {"tool": "calculator", "output": "4", "success": True},
            },
        ],
    )

    assert len(captured) == 3
    thought_row = captured[0]
    answer_row = captured[1]
    observation_row = captured[2]

    assert thought_row.step_type == "thought"
    assert thought_row.content == "先整理思路"

    assert answer_row.step_type == "answer"
    assert answer_row.content == "最终答案"

    assert observation_row.step_type == "observation"
    assert observation_row.content == "4"


@pytest.mark.asyncio
async def test_append_chat_run_event_persists_json_payload(monkeypatch):
    captured = []

    class _ScalarResult:
        def scalar(self):
            return 7

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, stmt):
            return _ScalarResult()

        def add(self, record):
            captured.append(record)

        async def commit(self):
            return None

    monkeypatch.setattr(runtime_module, "async_session_factory", lambda: _FakeSession())

    service = AgentRuntimeService()
    await service.append_chat_run_event(
        "run-chat",
        event="done",
        data={"answer": "最终答案", "extra": object()},
        created_at="2026-04-13T00:00:00",
    )

    assert len(captured) == 1
    row = captured[0]
    assert row.run_id == "run-chat"
    assert row.step_index == 8
    assert row.step_type == "chat_event"
    assert row.content == "最终答案"
    assert row.metadata_["event"] == "done"
    assert row.metadata_["payload"]["answer"] == "最终答案"
    assert isinstance(row.metadata_["payload"]["extra"], str)


@pytest.mark.asyncio
async def test_list_chat_run_events_loads_persisted_payloads(monkeypatch):
    from datetime import datetime
    from types import SimpleNamespace

    rows = [
        SimpleNamespace(
            metadata_={
                "event": "start",
                "payload": {"conversation_id": 42},
                "created_at": "2026-04-13T00:00:01",
            },
            created_at=datetime(2026, 4, 13, 0, 0, 1),
        ),
        SimpleNamespace(
            metadata_={
                "event": "done",
                "payload": {"answer": "ok"},
                "created_at": "2026-04-13T00:00:02",
            },
            created_at=datetime(2026, 4, 13, 0, 0, 2),
        ),
    ]

    class _Scalars:
        def all(self):
            return rows

    class _RowsResult:
        def scalars(self):
            return _Scalars()

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, stmt):
            return _RowsResult()

    monkeypatch.setattr(runtime_module, "async_session_factory", lambda: _FakeSession())

    service = AgentRuntimeService()
    events = await service.list_chat_run_events("run-chat")

    assert [item["event"] for item in events] == ["start", "done"]
    assert events[0]["data"]["conversation_id"] == 42
    assert events[1]["data"]["answer"] == "ok"


@pytest.mark.asyncio
async def test_cleanup_stale_runs_marks_old_running_runs_as_error(monkeypatch):
    old_running = SimpleNamespace(
        id="run-old",
        channel="chat",
        conversation_id=107,
        status="running",
        started_at=datetime.utcnow() - timedelta(hours=2),
        finished_at=None,
        metadata_={},
    )
    recent_running = SimpleNamespace(
        id="run-new",
        channel="chat",
        conversation_id=107,
        status="running",
        started_at=datetime.utcnow(),
        finished_at=None,
        metadata_={},
    )

    class _ScalarsResult:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return self

        def all(self):
            return list(self._rows)

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, stmt):
            return _ScalarsResult([old_running])

        async def commit(self):
            return None

    monkeypatch.setattr(runtime_module, "async_session_factory", lambda: _FakeSession())

    report = await AgentRuntimeService().cleanup_stale_runs(older_than_seconds=300, only_channels=["chat"])

    assert report["cleaned_count"] == 1
    assert report["cleaned_runs"][0]["id"] == "run-old"
    assert old_running.status == "error"
    assert old_running.finished_at is not None
    assert old_running.metadata_["error"] == "stale_run_cleanup"
    assert recent_running.status == "running"


def test_close_dangling_conversation_tool_calls_adds_failed_result():
    service = AgentRuntimeService()
    metadata = {
        "turn_store": {
            "version": "conversation_turn_store.v1",
            "entries": [
                {
                    "turn_id": "turn:1",
                    "status": "stopped",
                    "tool_call_count": 1,
                    "tool_result_count": 0,
                }
            ],
        },
        "tool_ledger": {
            "version": "conversation_tool_ledger.v1",
            "entries": [
                {
                    "entry_id": "call-entry",
                    "kind": "tool_call",
                    "tool_name": "knowledge_search",
                    "turn_id": "turn:1",
                    "tool_call_id": "call-1",
                    "run_id": "run-1",
                    "iteration": 1,
                    "status": "started",
                    "arguments": {"query": "classification"},
                }
            ],
        },
        "item_stream": {
            "version": "conversation_item_stream.v1",
            "entries": [
                {
                    "item_id": "item-call",
                    "kind": "tool_call",
                    "role": "tool",
                    "tool_name": "knowledge_search",
                    "turn_id": "turn:1",
                    "tool_call_id": "call-1",
                    "run_id": "run-1",
                    "iteration": 1,
                    "status": "started",
                    "arguments": {"query": "classification"},
                }
            ],
        },
    }

    updated, closed = service._close_dangling_conversation_tool_calls(
        metadata,
        running_run_ids=set(),
        non_running_run_ids={"run-1"},
        cleanup_at=datetime(2026, 4, 24, 8, 0, 0),
    )

    assert closed == [
        {
            "turn_id": "turn:1",
            "run_id": "run-1",
            "tool_call_id": "call-1",
            "tool_name": "knowledge_search",
        }
    ]
    item_results = [
        item
        for item in updated["item_stream"]["entries"]
        if item["kind"] == "tool_result" and item["tool_call_id"] == "call-1"
    ]
    ledger_results = [
        item
        for item in updated["tool_ledger"]["entries"]
        if item["kind"] == "tool_result" and item["tool_call_id"] == "call-1"
    ]
    assert item_results[0]["status"] == "failed"
    assert item_results[0]["success"] is False
    assert item_results[0]["error"] == "run_stopped_before_result"
    assert ledger_results[0]["status"] == "failed"
    assert updated["turn_store"]["entries"][0]["tool_call_count"] == 1
    assert updated["turn_store"]["entries"][0]["tool_result_count"] == 1
