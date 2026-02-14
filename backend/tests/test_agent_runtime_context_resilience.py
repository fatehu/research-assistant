import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.services.react_agent import AgentRuntimeContext, ReActAgent


class _SimpleLLM:
    provider = "test"
    config = {"model": "test-model"}

    def supports_function_calling(self):
        return False

    async def chat(self, *args, **kwargs):
        return {
            "content": "<answer>ok</answer>",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


class _BrokenIntentTools:
    def classify_intent(self, user_text: str):
        raise RuntimeError("intent classify failed")

    def select_tool_names_for_intent(self, intent: str, user_text: str = ""):
        raise RuntimeError("intent select failed")

    def get_tools_description(self, **kwargs):
        return ""

    def list_tools(self, **kwargs):
        return []


class _RuntimeRecorder:
    def __init__(self):
        self.created = []
        self.completed = []
        self.steps = []

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

    async def upsert_conversation_summary(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_prepare_runtime_context_intent_failures_do_not_abort_run(monkeypatch):
    monkeypatch.setattr(settings, "agent_persist_steps_enabled", True)
    runtime = _RuntimeRecorder()
    agent = ReActAgent(
        _SimpleLLM(),
        _BrokenIntentTools(),
        max_iterations=1,
        runtime_context=AgentRuntimeContext(user_id=1, channel="chat", conversation_id=42),
        runtime_service=runtime,
    )

    events = []
    async for event in agent.run([{"role": "user", "content": "hello"}], stream=False):
        events.append(event)

    assert any(e.get("type") == "done" for e in events)
    assert runtime.created
    assert runtime.created[0]["intent"] == "general_chat"
    assert runtime.created[0]["selected_tools"] == []
