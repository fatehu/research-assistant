import os
import sys

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
