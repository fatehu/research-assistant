import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.services.react_agent import ReActAgent


class _BudgetLLM:
    provider = "test"
    config = {"model": "test-model"}

    def __init__(self):
        self.seen_messages = []

    def supports_function_calling(self):
        return False

    async def chat(self, messages, system_prompt=None, **kwargs):
        self.seen_messages = list(messages)
        return {"content": "<answer>预算控制完成</answer>"}


class _BudgetTools:
    def get_tools_description(self, **kwargs):
        return "- calculator: 计算器"


@pytest.mark.asyncio
async def test_context_budget_trims_history_and_strips_think(monkeypatch):
    monkeypatch.setattr(settings, "agent_context_budget_enabled", True)
    monkeypatch.setattr(settings, "agent_context_max_input_tokens", 1400)
    monkeypatch.setattr(settings, "agent_context_window_turns", 2)
    monkeypatch.setattr(settings, "agent_context_summary_trigger_tokens", 128)

    llm = _BudgetLLM()
    agent = ReActAgent(llm, _BudgetTools(), max_iterations=1)

    messages = []
    for i in range(12):
        messages.append({"role": "user", "content": f"用户历史问题 {i} " + ("A" * 120)})
        messages.append(
            {
                "role": "assistant",
                "content": f"<think>历史思考{i}</think>历史回答 {i} " + ("B" * 120),
            }
        )
    messages.append({"role": "user", "content": "当前问题是什么？"})

    events = []
    async for event in agent.run(messages, stream=False):
        events.append(event)

    assert len(llm.seen_messages) < len(messages)
    assistant_contents = [
        str(item.get("content", ""))
        for item in llm.seen_messages
        if str(item.get("role", "")).lower() == "assistant"
    ]
    assert all("<think>" not in content for content in assistant_contents)
    assert any(event.get("type") == "done" for event in events)
