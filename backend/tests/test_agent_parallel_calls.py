import os
import sys
import time
import asyncio

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.services.agent_tools import ToolResult
from app.services.react_agent import ReActAgent


class _ParallelLLM:
    provider = "test"
    config = {"model": "test-model"}

    def __init__(self):
        self.calls = 0

    def supports_function_calling(self):
        return True

    async def chat_with_tools(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return {
                "content": "并行执行两个工具",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "slow_tool",
                        "arguments": '{"value": 1}',
                    },
                    {
                        "id": "call-2",
                        "name": "slow_tool",
                        "arguments": '{"value": 2}',
                    },
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        return {
            "content": "<answer>并行完成</answer>",
            "tool_calls": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


class _SafeTool:
    parallel_safe = True


class _UnsafeTool:
    pass


class _ParallelTools:
    def get_tools_description(self, **kwargs):
        return "- slow_tool: 慢工具"

    def list_tools(self, **kwargs):
        return [
            {
                "type": "function",
                "function": {
                    "name": "slow_tool",
                    "description": "slow tool",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "integer"}},
                        "required": ["value"],
                    },
                },
            }
        ]

    def get(self, name: str):
        if name == "slow_tool":
            return _SafeTool()
        return None

    async def execute(self, tool_name: str, **kwargs):
        await asyncio.sleep(0.25)
        return ToolResult(success=True, output=f"value={kwargs.get('value')}")


class _SerialTools(_ParallelTools):
    def get(self, name: str):
        if name == "slow_tool":
            return _UnsafeTool()
        return None


@pytest.mark.asyncio
async def test_parallel_tool_calls_reduce_latency(monkeypatch):
    monkeypatch.setattr(settings, "agent_parallel_tool_calls_enabled", True)
    monkeypatch.setattr(settings, "agent_parallel_tool_calls_max_concurrency", 4)
    agent = ReActAgent(_ParallelLLM(), _ParallelTools(), max_iterations=3)

    started = time.perf_counter()
    events = []
    async for event in agent.run([{"role": "user", "content": "并行测一下"}], stream=False):
        events.append(event)
    elapsed = time.perf_counter() - started

    observations = [e for e in events if e.get("type") == "observation"]
    done = [e for e in events if e.get("type") == "done"]

    assert len(observations) == 2
    assert len(done) == 1
    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_parallel_tool_calls_require_explicit_parallel_safe(monkeypatch):
    monkeypatch.setattr(settings, "agent_parallel_tool_calls_enabled", True)
    monkeypatch.setattr(settings, "agent_parallel_tool_calls_max_concurrency", 4)
    agent = ReActAgent(_ParallelLLM(), _SerialTools(), max_iterations=3)

    started = time.perf_counter()
    events = []
    async for event in agent.run([{"role": "user", "content": "并行测一下"}], stream=False):
        events.append(event)
    elapsed = time.perf_counter() - started

    observations = [e for e in events if e.get("type") == "observation"]
    done = [e for e in events if e.get("type") == "done"]

    assert len(observations) == 2
    assert len(done) == 1
    assert elapsed >= 0.45
