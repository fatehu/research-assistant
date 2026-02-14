import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.react_agent import ReActAgent


class _AnswerLLM:
    provider = "test"
    config = {"model": "test-model"}

    def supports_function_calling(self):
        return False

    async def chat(self, *args, **kwargs):
        return {
            "content": "<answer>ok</answer>",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


class _RefreshTools:
    def __init__(self):
        self.refresh_calls = 0

    async def refresh_mcp_tools(self, force_refresh: bool = False):
        self.refresh_calls += 1

    def classify_intent(self, user_text: str) -> str:
        return "general_chat"

    def select_tool_names_for_intent(self, intent: str, user_text: str = ""):
        return []

    def get_tools_description(self, **kwargs):
        return ""

    def list_tools(self, **kwargs):
        return []


@pytest.mark.asyncio
async def test_agent_run_refreshes_mcp_tools_once():
    tools = _RefreshTools()
    agent = ReActAgent(_AnswerLLM(), tools, max_iterations=1)

    events = []
    async for event in agent.run([{"role": "user", "content": "hello"}], stream=False):
        events.append(event)

    assert any(e.get("type") == "done" for e in events)
    assert tools.refresh_calls == 1
