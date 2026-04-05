import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.services.react_agent import AgentContext, ReActAgent


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
    monkeypatch.setattr(settings, "agent_context_recently_slid_turns", 2)
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
    system_contents = [
        str(item.get("content", ""))
        for item in llm.seen_messages
        if str(item.get("role", "")).lower() == "system"
    ]
    assert all("<think>" not in content for content in assistant_contents)
    assert not any(content.startswith("关键历史锚点：") for content in system_contents)
    context_debug_events = [event for event in events if event.get("type") == "context_debug"]
    assert context_debug_events
    payload = context_debug_events[-1]["data"]
    assert payload["version"] == "chat_context_debug.v1"
    assert payload["window_turns"] == 2
    assert str(payload.get("anchor_summary", "")) == ""
    assert payload["recently_slid_messages_count"] > 0
    assert isinstance(payload["recent_messages"], list)
    assert any(event.get("type") == "done" for event in events)


@pytest.mark.asyncio
async def test_context_budget_trims_observation_after_first_user_turn(monkeypatch):
    monkeypatch.setattr(settings, "agent_context_budget_enabled", True)
    monkeypatch.setattr(settings, "agent_context_max_input_tokens", 256)
    monkeypatch.setattr(settings, "agent_context_window_turns", 8)
    monkeypatch.setattr(settings, "agent_context_recently_slid_turns", 2)

    agent = ReActAgent(_BudgetLLM(), _BudgetTools(), max_iterations=1)
    context = AgentContext(
        messages=[
            {"role": "user", "content": "请总结上传文档"},
            {"role": "tool", "content": "X" * 12000},
        ]
    )

    trimmed = await agent._prepare_llm_messages(context, system_prompt="system")
    roles = [str(item.get("role", "")).lower() for item in trimmed]

    assert "user" in roles
    assert "tool" not in roles
    assert context.context_truncated is True
    assert context.context_debug["context_truncated"] is True
    assert context.context_debug["message_count_sent"] == len(trimmed)


@pytest.mark.asyncio
async def test_context_budget_emits_anchor_when_messages_just_slide_out(monkeypatch):
    monkeypatch.setattr(settings, "agent_context_budget_enabled", True)
    monkeypatch.setattr(settings, "agent_context_max_input_tokens", 4096)
    monkeypatch.setattr(settings, "agent_context_window_turns", 2)
    monkeypatch.setattr(settings, "agent_context_recently_slid_turns", 1)
    monkeypatch.setattr(settings, "agent_context_summary_trigger_tokens", 999999)

    agent = ReActAgent(_BudgetLLM(), _BudgetTools(), max_iterations=1)
    context = AgentContext(
        messages=[
            {"role": "user", "content": "最早的问题：解释注意力机制的本质。"},
            {"role": "assistant", "content": "最早的回答。"},
            {"role": "user", "content": "第二轮：创新点在哪？"},
            {"role": "assistant", "content": "第二轮回答。"},
            {"role": "user", "content": "第三轮：为什么以前没发现？"},
        ]
    )

    prepared = await agent._prepare_llm_messages(context, system_prompt="system")

    assert prepared
    assert context.context_debug["older_messages_count"] == 0
    assert context.context_debug["recently_slid_messages_count"] == 2
    assert str(context.context_debug.get("anchor_summary", "")) == ""
    assert str(context.context_debug.get("older_history_summary", "")) == ""


@pytest.mark.asyncio
async def test_context_budget_can_summarize_recently_slid_history_when_tokens_high(monkeypatch):
    monkeypatch.setattr(settings, "agent_context_budget_enabled", True)
    monkeypatch.setattr(settings, "agent_context_max_input_tokens", 8192)
    monkeypatch.setattr(settings, "agent_context_window_turns", 2)
    monkeypatch.setattr(settings, "agent_context_recently_slid_turns", 1)
    monkeypatch.setattr(settings, "agent_context_summary_trigger_tokens", 64)

    agent = ReActAgent(_BudgetLLM(), _BudgetTools(), max_iterations=1)
    long_tail = "A" * 600
    context = AgentContext(
        messages=[
            {"role": "user", "content": f"最早的问题：解释注意力机制的本质。{long_tail}"},
            {"role": "assistant", "content": f"最早的回答。{long_tail}"},
            {"role": "user", "content": f"第二轮：创新点在哪？{long_tail}"},
            {"role": "assistant", "content": f"第二轮回答。{long_tail}"},
            {"role": "user", "content": "第三轮：为什么以前没发现？"},
        ]
    )

    prepared = await agent._prepare_llm_messages(context, system_prompt="system")

    assert prepared
    assert context.context_debug["older_messages_count"] == 0
    assert context.context_debug["recently_slid_messages_count"] == 2
    assert str(context.context_debug.get("anchor_summary", "")) == ""
    assert "user:" in str(context.context_debug.get("older_history_summary", "")).lower()


def test_summarize_messages_prefers_thought_for_assistant_history():
    summary = ReActAgent._summarize_messages(
        [
            {"role": "user", "content": "解释一下 agentic search。"},
            {
                "role": "assistant",
                "content": "这是一个很长的最终答案正文。",
                "thought": "先检索知识库定义，再对比传统 RAG，最后收束成核心差异。",
            },
        ],
        max_lines=4,
    )

    assert "推理摘要" in summary
    assert "先检索知识库定义" in summary

@pytest.mark.asyncio
async def test_prepare_llm_messages_includes_conversation_context_state_prefix(monkeypatch):
    monkeypatch.setattr(settings, "agent_context_budget_enabled", True)
    monkeypatch.setattr(settings, "agent_context_max_input_tokens", 4096)

    agent = ReActAgent(_BudgetLLM(), _BudgetTools(), max_iterations=1)
    context = AgentContext(
        messages=[{"role": "user", "content": "当前问题是什么？"}],
        conversation_state={
            "version": "conversation_context_state.v3",
            "active_topic": "注意力机制",
            "user_goal": "解释注意力机制为什么以前没被提出",
            "constraints": ["用中文说明"],
            "open_questions": ["为什么以前没发现"],
            "resolved_facts": ["Bahdanau 2014 首次提出注意力机制"],
            "evidence_ledger": [
                {
                    "summary": "已检索 attention mechanism 定义",
                    "status": "confirmed",
                    "source_labels": ["来源1"],
                    "tool_names": ["knowledge_search"],
                }
            ],
            "last_reasoning_summary": "先解释背景，再回答历史条件。",
            "turn_count": 3,
        },
    )

    prepared = await agent._prepare_llm_messages(context, system_prompt="system")
    system_texts = [
        str(item.get("content", ""))
        for item in prepared
        if str(item.get("role", "")).lower() == "system"
    ]

    assert any(text.startswith("会话上下文状态：") for text in system_texts)
    assert "注意力机制" in str(context.context_debug.get("conversation_state_summary", ""))


@pytest.mark.asyncio
async def test_run_prefers_reasoning_summary_as_done_thought(monkeypatch):
    monkeypatch.setattr(settings, "agent_reasoning_summary_enabled", True)

    llm = _BudgetLLM()
    agent = ReActAgent(llm, _BudgetTools(), max_iterations=1)

    async def _fake_reasoning_summary(context):
        return "先基于已有上下文确认问题，再直接收束为最终结论。"

    monkeypatch.setattr(agent, "_generate_reasoning_summary", _fake_reasoning_summary)

    events = []
    async for event in agent.run([{"role": "user", "content": "当前问题是什么？"}], stream=False):
        events.append(event)

    done_event = next(event for event in events if event.get("type") == "done")
    assert done_event["data"]["thought"] == "先基于已有上下文确认问题，再直接收束为最终结论。"
    assert done_event["data"]["reasoning_summary"] == "先基于已有上下文确认问题，再直接收束为最终结论。"
