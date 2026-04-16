import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.services.react_agent import AgentRuntimeContext, ReActAgent


class _PersistLLM:
    provider = "test"
    config = {"model": "test-model"}

    def supports_function_calling(self):
        return False

    async def chat(self, *args, **kwargs):
        return {"content": "<answer>已完成</answer>", "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}


class _PersistTools:
    def get_tools_description(self, **kwargs):
        return "- calculator: 计算器"

    def classify_intent(self, user_text: str):
        return "general_chat"

    def select_tool_names_for_intent(self, intent: str, user_text: str = ""):
        return ["calculator"]


class _FakeRuntimeService:
    def __init__(self):
        self.created = []
        self.steps = []
        self.completed = []

    async def create_run(self, **kwargs):
        self.created.append(kwargs)
        return "run-1"

    async def append_steps(self, run_id, steps):
        self.steps.append((run_id, list(steps)))

    async def complete_run(self, run_id, **kwargs):
        self.completed.append((run_id, kwargs))

    async def recall(self, **kwargs):
        return []

    async def remember(self, **kwargs):
        return None


@pytest.mark.asyncio
async def test_agent_step_persistence_enabled(monkeypatch):
    monkeypatch.setattr(settings, "agent_persist_steps_enabled", True)

    runtime = _FakeRuntimeService()
    agent = ReActAgent(
        _PersistLLM(),
        _PersistTools(),
        max_iterations=1,
        runtime_context=AgentRuntimeContext(user_id=1, channel="chat", conversation_id=100),
        runtime_service=runtime,
    )

    events = []
    async for event in agent.run([{"role": "user", "content": "你好"}], stream=False):
        events.append(event)

    assert runtime.created
    assert runtime.steps
    run_id, step_events = runtime.steps[0]
    assert run_id == "run-1"
    assert any(step.get("type") == "answer" for step in step_events)
    assert runtime.completed
    assert runtime.completed[0][1]["status"] == "success"
    assert any(event.get("type") == "done" for event in events)


@pytest.mark.asyncio
async def test_literature_run_persistence_uses_explicit_scope_without_conversation_binding(monkeypatch):
    monkeypatch.setattr(settings, "agent_persist_steps_enabled", True)

    runtime = _FakeRuntimeService()
    agent = ReActAgent(
        _PersistLLM(),
        _PersistTools(),
        max_iterations=1,
        runtime_context=AgentRuntimeContext(
            user_id=1,
            channel="literature",
            conversation_id=321,
            scope_type="literature_session",
            scope_id="321",
        ),
        runtime_service=runtime,
    )

    async for _event in agent.run([{"role": "user", "content": "解释这篇论文"}], stream=False):
        pass

    assert runtime.created
    created = runtime.created[0]
    assert created["conversation_id"] is None
    assert created["metadata"]["scope_type"] == "literature_session"
    assert created["metadata"]["scope_id"] == "321"
