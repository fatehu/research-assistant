import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.services.agent_tools import ToolResult
from app.services.react_agent import ReActAgent


class _FallbackLLM:
    provider = "test"
    config = {"model": "test-model"}

    def __init__(self):
        self.chat_calls = 0

    def supports_function_calling(self):
        return True

    async def chat_with_tools(self, *args, **kwargs):
        raise RuntimeError("provider function-calling failed")

    async def chat(self, *args, **kwargs):
        self.chat_calls += 1
        if self.chat_calls == 1:
            return {
                "content": '<think>先算一下</think><action>{"tool":"calculator","input":{"expression":"2+2"}}</action>'
            }
        return {"content": "<think>完成</think><answer>结果是 4</answer>"}


class _FallbackTools:
    def get_tools_description(self, **kwargs):
        return "- calculator: 计算器"

    async def execute(self, tool_name: str, **kwargs):
        assert tool_name == "calculator"
        return ToolResult(success=True, output="4", data={"result": 4})


class _DirectAnswerFCLLM:
    provider = "test"
    config = {"model": "test-model"}

    def supports_function_calling(self):
        return True

    async def chat_with_tools(self, *args, **kwargs):
        return {
            "content": "这是一个无需调用工具的直接回答。",
            "reasoning": "",
            "tool_calls": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }

    async def chat(self, *args, **kwargs):
        return {"content": "<answer>fallback</answer>"}


class _NoopTools:
    def get_tools_description(self, **kwargs):
        return "- datetime: 时间"

    def list_tools(self, **kwargs):
        return []

    async def execute(self, tool_name: str, **kwargs):
        raise AssertionError("no tool call expected")


@pytest.mark.asyncio
async def test_function_calling_fallback_to_xml(monkeypatch):
    monkeypatch.setattr(settings, "agent_function_calling_fallback_xml", True)
    agent = ReActAgent(_FallbackLLM(), _FallbackTools(), max_iterations=3)

    events = []
    async for event in agent.run([{"role": "user", "content": "2+2 等于多少"}], stream=False):
        events.append(event)

    action_events = [e for e in events if e.get("type") == "action"]
    observation_events = [e for e in events if e.get("type") == "observation"]
    done_events = [e for e in events if e.get("type") == "done"]

    assert len(action_events) >= 1
    assert len(observation_events) >= 1
    assert len(done_events) == 1
    assert "4" in done_events[0]["data"]["answer"]


@pytest.mark.asyncio
async def test_function_calling_direct_answer_emits_thought_step():
    agent = ReActAgent(_DirectAnswerFCLLM(), _NoopTools(), max_iterations=1)

    events = []
    async for event in agent.run([{"role": "user", "content": "直接回答"}], stream=False):
        events.append(event)

    thought_events = [event for event in events if event.get("type") == "thought"]
    done_events = [event for event in events if event.get("type") == "done"]

    assert thought_events
    assert "问题分析" in str(thought_events[0].get("data", ""))
    assert done_events and "直接回答" in str(done_events[0]["data"]["answer"])


def test_plain_chat_normalization_strips_tool_protocol_messages():
    messages = [
        {"role": "user", "content": "用户问题"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "paper_read", "arguments": "{\"query\":\"Fig 3\"}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "paper_read",
            "content": "paper observation",
        },
    ]

    normalized = ReActAgent._normalize_messages_for_plain_chat(messages)

    assert normalized == [
        {"role": "user", "content": "用户问题"},
        {"role": "user", "content": "<observation>\npaper observation\n</observation>"},
    ]


def test_function_calling_normalization_preserves_complete_tool_call_groups():
    messages = [
        {"role": "user", "content": "用户问题"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "paper_read", "arguments": "{\"query\":\"Fig 3\"}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "paper_read",
            "content": "paper observation",
        },
    ]

    normalized = ReActAgent._normalize_messages_for_function_calling(messages)

    assert normalized == [
        {"role": "user", "content": "用户问题"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "paper_read", "arguments": "{\"query\":\"Fig 3\"}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "paper_read",
            "content": "paper observation",
        },
    ]


def test_function_calling_normalization_downgrades_broken_tool_call_groups():
    messages = [
        {"role": "user", "content": "用户问题"},
        {
            "role": "assistant",
            "content": "先查一下",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "paper_read", "arguments": "{\"query\":\"Fig 3\"}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_other",
            "name": "paper_read",
            "content": "orphan observation",
        },
    ]

    normalized = ReActAgent._normalize_messages_for_function_calling(messages)

    assert normalized == [
        {"role": "user", "content": "用户问题"},
        {"role": "assistant", "content": "先查一下"},
        {"role": "user", "content": "<observation>\norphan observation\n</observation>"},
    ]
