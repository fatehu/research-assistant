import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.services.agent_tools import ToolResult
from app.services.react_agent import AgentContext, AgentRuntimeContext, ParsedToolCall, ReActAgent


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


class _ThinkingAliasDirectAnswerFCLLM:
    provider = "test"
    config = {"model": "test-model"}

    def supports_function_calling(self):
        return True

    async def chat_with_tools(self, *args, **kwargs):
        return {
            "content": "<thinking>先判断问题无需工具</thinking>这是直接答案。",
            "reasoning": "",
            "tool_calls": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }

    async def chat(self, *args, **kwargs):
        return {"content": "<answer>fallback</answer>"}


class _CaptureToolChoiceFCLLM:
    provider = "test"
    config = {"model": "test-model"}

    def __init__(self):
        self.captured_tool_choice = None

    def supports_function_calling(self):
        return True

    async def chat_with_tools(self, *args, **kwargs):
        self.captured_tool_choice = kwargs.get("tool_choice")
        return {
            "content": "",
            "reasoning": "先检索知识库",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "name": "knowledge_search",
                    "arguments": "{\"query\":\"agentic search\"}",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }

    async def chat(self, *args, **kwargs):
        return {"content": "<answer>fallback</answer>"}


class _AnswerDraftToolCallFCLLM:
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
                "content": "",
                "reasoning": (
                    "让我先给出关键里程碑：\n"
                    "## 核心节点\n"
                    "- 2014年：注意力机制进入机器翻译\n"
                    "- 2017年：Transformer 发布\n"
                    "- 2018年：BERT 推动预训练范式\n"
                    "具体来说，这些节点说明了为什么后续大模型能力会快速增强。"
                ),
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "name": "datetime",
                        "arguments": "{\"query\":\"2014 到现在多少年\"}",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            }
        return {
            "content": "关键里程碑如下：2014、2017、2018。",
            "reasoning": "",
            "tool_calls": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }

    async def chat(self, *args, **kwargs):
        return {"content": "<answer>fallback</answer>"}


class _CaptureMultiTurnToolChoiceFCLLM:
    provider = "test"
    config = {"model": "test-model"}

    def __init__(self):
        self.captured_tool_choices = []
        self.calls = 0

    def supports_function_calling(self):
        return True

    async def chat_with_tools(self, *args, **kwargs):
        self.calls += 1
        self.captured_tool_choices.append(kwargs.get("tool_choice"))
        if self.calls == 1:
            return {
                "content": "",
                "reasoning": "先检索知识库",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "name": "knowledge_search",
                        "arguments": "{\"query\":\"agentic search\"}",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            }
        return {
            "content": "Agentic search 会在检索过程中自主规划下一步，并结合工具与反馈迭代决策 [来源1]。",
            "reasoning": "已有足够证据，直接回答",
            "tool_calls": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }

    async def chat(self, *args, **kwargs):
        return {"content": "<answer>fallback</answer>"}


class _RedundantKnowledgeSearchFCLLM:
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
                "content": "",
                "reasoning": "先做一次知识库检索",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "name": "knowledge_search",
                        "arguments": "{\"query\":\"agentic search 智能体搜索\"}",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            }
        if self.calls == 2:
            return {
                "content": "",
                "reasoning": "再换个说法搜一次",
                "tool_calls": [
                    {
                        "id": "call_2",
                        "type": "function",
                        "name": "knowledge_search",
                        "arguments": "{\"query\":\"agentic search 定义 特点 与传统RAG区别\"}",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            }
        return {
            "content": "Agentic search 会在检索过程中自主规划下一步，并结合反馈调整策略 [来源1]。",
            "reasoning": "已有足够证据，直接给出答案",
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


class _KnowledgeIntentTools:
    def classify_intent(self, user_text: str) -> str:
        return "knowledge_query"

    def select_tool_names_for_intent(self, intent: str, user_text: str = ""):
        return ["knowledge_search", "datetime", "calculator"]

    def get_tools_description(self, **kwargs):
        return "- knowledge_search: 搜索知识库"

    def list_tools(self, **kwargs):
        names = set(kwargs.get("include_tool_names") or {"knowledge_search"})
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": name,
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                },
            }
            for name in sorted(names)
        ]

    async def execute(self, tool_name: str, **kwargs):
        assert tool_name == "knowledge_search"
        return ToolResult(success=True, output="[来源1] 检索命中", data={"results": [{"content": "agentic search"}]})


class _DateTimeOnlyTools:
    def get_tools_description(self, **kwargs):
        return "- datetime: 时间计算"

    def list_tools(self, **kwargs):
        return [
            {
                "type": "function",
                "function": {
                    "name": "datetime",
                    "description": "datetime",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                },
            }
        ]

    async def execute(self, tool_name: str, **kwargs):
        assert tool_name == "datetime"
        return ToolResult(success=True, output="2014 距今约 12 年。")


class _RepeatedFailureLLM:
    provider = "test"
    config = {"model": "test-model"}

    def supports_function_calling(self):
        return False

    async def chat(self, *args, **kwargs):
        return {
            "content": '<think>继续修复</think><action>{"tool":"notebook_execute","input":{"code":"print(1)"}}</action>',
            "usage": {"prompt_tokens": 8, "completion_tokens": 8, "total_tokens": 16},
        }


class _RepeatedFailureTools:
    def get_tools_description(self, **kwargs):
        return "- notebook_execute: 执行 notebook cell"

    async def execute(self, tool_name: str, **kwargs):
        assert tool_name == "notebook_execute"
        return ToolResult(success=False, output="PolicyViolationError: 不要导入 os", error="policy_violation")


class _SimpleExecuteTools:
    def get_tools_description(self, **kwargs):
        return "- datetime: 时间计算"

    def get(self, _name: str):
        return None

    async def execute(self, tool_name: str, **kwargs):
        assert tool_name == "datetime"
        assert kwargs["query"] == "2014 到现在多少年"
        return ToolResult(
            success=True,
            output="Bahdanau 2014 引入注意力机制。",
            data={"result": "12 年"},
            execution_time_ms=12.5,
            output_tokens_estimate=18,
        )


class _ToolLedgerRuntimeService:
    def __init__(self):
        self.entries = []
        self.item_entries = []

    async def append_conversation_tool_ledger_entries(self, conversation_id: int, entries):
        assert conversation_id == 54
        self.entries.extend(list(entries or []))

    async def append_conversation_item_entries(self, conversation_id: int, entries):
        assert conversation_id == 54
        self.item_entries.extend(list(entries or []))



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


@pytest.mark.asyncio
async def test_function_calling_direct_answer_extracts_thinking_alias_into_thought():
    agent = ReActAgent(_ThinkingAliasDirectAnswerFCLLM(), _NoopTools(), max_iterations=1)

    events = []
    async for event in agent.run([{"role": "user", "content": "直接回答"}], stream=False):
        events.append(event)

    thought_events = [event for event in events if event.get("type") == "thought"]
    done_events = [event for event in events if event.get("type") == "done"]

    assert thought_events and thought_events[0]["data"] == "先判断问题无需工具"
    assert done_events and done_events[0]["data"]["answer"] == "这是直接答案。"


@pytest.mark.asyncio
async def test_function_calling_tool_plan_redacts_answer_like_reasoning_into_process_summary():
    agent = ReActAgent(_AnswerDraftToolCallFCLLM(), _DateTimeOnlyTools(), max_iterations=2)

    events = []
    async for event in agent.run([{"role": "user", "content": "帮我梳理注意力机制时间线"}], stream=False):
        events.append(event)

    thought_events = [event for event in events if event.get("type") == "thought"]
    answer_event = next(event for event in events if event.get("type") == "answer")

    assert thought_events
    assert "调用 `datetime`" in str(thought_events[0]["data"])
    assert "关键里程碑" not in str(thought_events[0]["data"])
    assert "2014" in str(answer_event["data"])


@pytest.mark.asyncio
async def test_function_calling_uses_auto_tool_choice_with_available_tools():
    llm = _CaptureToolChoiceFCLLM()
    agent = ReActAgent(llm, _KnowledgeIntentTools(), max_iterations=1)

    events = []
    async for event in agent.run([{"role": "user", "content": "利用知识库解释 agentic search"}], stream=False):
        events.append(event)

    assert llm.captured_tool_choice == "auto"
    assert any(event.get("type") == "action" for event in events)


@pytest.mark.asyncio
async def test_function_calling_keeps_auto_tool_choice_after_first_knowledge_observation():
    llm = _CaptureMultiTurnToolChoiceFCLLM()
    agent = ReActAgent(llm, _KnowledgeIntentTools(), max_iterations=3)

    events = []
    async for event in agent.run([{"role": "user", "content": "利用知识库解释 agentic search"}], stream=False):
        events.append(event)

    done_events = [event for event in events if event.get("type") == "done"]

    assert llm.captured_tool_choices[:2] == ["auto", "auto"]
    assert done_events and "[来源1]" in str(done_events[0]["data"]["answer"])


@pytest.mark.asyncio
async def test_function_calling_blocks_redundant_knowledge_search_after_success():
    llm = _RedundantKnowledgeSearchFCLLM()
    agent = ReActAgent(llm, _KnowledgeIntentTools(), max_iterations=4)

    events = []
    async for event in agent.run([{"role": "user", "content": "利用知识库解释 agentic search"}], stream=False):
        events.append(event)

    action_events = [event for event in events if event.get("type") == "action"]
    thought_events = [event for event in events if event.get("type") == "thought"]
    done_events = [event for event in events if event.get("type") == "done"]

    assert len(action_events) == 1
    assert any("重复知识库搜索" in str(event.get("data", "")) for event in thought_events)
    assert done_events and "[来源1]" in str(done_events[0]["data"]["answer"])


@pytest.mark.asyncio
async def test_agent_stops_after_repeated_same_tool_failures(monkeypatch):
    monkeypatch.setattr(settings, "agent_tool_failure_streak_limit", 3, raising=False)
    agent = ReActAgent(_RepeatedFailureLLM(), _RepeatedFailureTools(), max_iterations=8)

    events = []
    async for event in agent.run([{"role": "user", "content": "继续修复这个 notebook"}], stream=False):
        events.append(event)

    action_events = [event for event in events if event.get("type") == "action"]
    done_events = [event for event in events if event.get("type") == "done"]

    assert len(action_events) == 3
    assert done_events
    assert done_events[0]["data"]["iterations"] == 3
    assert "已停止自动重试" in str(done_events[0]["data"]["answer"])


@pytest.mark.asyncio
async def test_execute_tool_calls_persists_tool_ledger_entries():
    runtime_service = _ToolLedgerRuntimeService()
    agent = ReActAgent(
        llm_service=_DirectAnswerFCLLM(),
        tool_registry=_SimpleExecuteTools(),
        runtime_context=AgentRuntimeContext(user_id=7, channel="chat", conversation_id=54),
        runtime_service=runtime_service,
    )
    context = AgentContext(
        messages=[{"role": "user", "content": "解释注意力机制"}],
        iteration=1,
        run_id="run-1",
    )

    executed = await agent._execute_tool_calls(
        context,
        [
            ParsedToolCall(
                call_id="call_1",
                name="datetime",
                arguments={"query": "2014 到现在多少年"},
                arguments_raw='{"query":"2014 到现在多少年"}',
            )
        ],
    )

    assert len(executed) == 1
    assert len(runtime_service.entries) == 2
    assert runtime_service.entries[0]["kind"] == "tool_call"
    assert runtime_service.entries[0]["tool_name"] == "datetime"
    assert runtime_service.entries[1]["kind"] == "tool_result"
    assert runtime_service.entries[1]["status"] == "succeeded"
    assert runtime_service.entries[1]["success"] is True
    assert "Bahdanau 2014" in str(runtime_service.entries[1]["summary"])
    assert runtime_service.item_entries
    assert runtime_service.item_entries[0]["kind"] == "tool_use_summary"
    assert runtime_service.item_entries[0]["turn_id"] is None
    assert "datetime" in str(runtime_service.item_entries[0]["summary"])


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
