import os
import sys
import json

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app.services.conversation_context_compaction_service as compaction_module
from app.config import settings
from app.services.chat_context_store import ConversationItemStreamStore
from app.services.react_agent import AgentContext, AgentRuntimeContext, ReActAgent


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


class _CompactionLLM:
    provider = "test"
    config = {"model": "fake-context-state-model"}

    def __init__(self, provider=None):
        if provider:
            self.provider = provider

    async def chat(self, messages, system_prompt=None, **kwargs):
        if "会话历史压缩器" in str(system_prompt or ""):
            return {
                "content": json.dumps(
                    {
                        "history_anchors": "此前已经解释旧问题。",
                        "history_summary": "旧历史已经被压缩，当前轮继续围绕当前问题展开。",
                        "replacement_history": [
                            {"role": "system", "content": "此前已经解释旧问题。"},
                        ],
                    },
                    ensure_ascii=False,
                )
            }
        return {
            "content": json.dumps(
                {
                    "active_topic": "当前问题",
                    "user_goal": "继续完成当前问题",
                    "constraints": [],
                    "open_questions": ["当前问题还没完全回答"],
                    "resolved_facts": ["旧问题已解释"],
                    "evidence_ledger": [],
                    "last_reasoning_summary": "已压缩旧历史，保留当前轮任务。",
                },
                ensure_ascii=False,
            )
        }


class _MidRunRuntime(_RuntimeRecorder):
    def __init__(self):
        super().__init__()
        self.context_states = []
        self.compacted_histories = []
        self.history_events = []
        self.snapshots = []
        self.tool_ledger_payload = {
            "version": "conversation_tool_ledger.v1",
            "updated_at": "2026-04-02T00:00:00",
            "entries": [
                {
                    "entry_id": "ledger-1",
                    "kind": "tool_result",
                    "tool_name": "knowledge_search",
                    "turn_id": "turn:200",
                    "tool_call_id": "call-1",
                    "summary": "查到了当前问题相关结论。",
                    "success": True,
                }
            ],
        }
        self.item_stream_payload = {
            "version": "conversation_item_stream.v1",
            "updated_at": "2026-04-02T00:00:00",
            "entries": [
                {
                    "item_id": "item-1",
                    "kind": "user_message",
                    "turn_id": "turn:100",
                    "role": "user",
                    "content": "旧问题",
                    "message_id": 100,
                },
                {
                    "item_id": "item-2",
                    "kind": "assistant_message",
                    "turn_id": "turn:100",
                    "role": "assistant",
                    "content": "旧回答",
                    "message_id": 101,
                },
                {
                    "item_id": "item-3",
                    "kind": "user_message",
                    "turn_id": "turn:200",
                    "role": "user",
                    "content": "当前问题",
                    "message_id": 200,
                },
                {
                    "item_id": "item-4",
                    "kind": "tool_result",
                    "turn_id": "turn:200",
                    "role": "tool",
                    "tool_name": "knowledge_search",
                    "tool_call_id": "call-1",
                    "summary": "查到了当前问题相关结论。",
                    "success": True,
                },
            ],
        }

    async def get_conversation_item_stream(self, conversation_id: int):
        return dict(self.item_stream_payload)

    async def get_conversation_tool_ledger(self, conversation_id: int):
        return dict(self.tool_ledger_payload)

    async def upsert_conversation_context_state(self, conversation_id: int, state):
        self.context_states.append((conversation_id, dict(state)))

    async def upsert_conversation_compacted_history(self, conversation_id: int, compacted_history):
        self.compacted_histories.append((conversation_id, dict(compacted_history)))

    async def append_conversation_history_event(self, conversation_id: int, *, title: str, detail: str):
        self.history_events.append((conversation_id, title, detail))

    async def append_conversation_context_snapshot(self, conversation_id: int, snapshot):
        self.snapshots.append((conversation_id, dict(snapshot)))

    async def append_conversation_item_entries(self, conversation_id: int, entries):
        store = ConversationItemStreamStore.from_payload(self.item_stream_payload)
        store.extend(
            [
                compaction_module.ConversationItemStreamStore.from_payload(
                    {"version": "conversation_item_stream.v1", "entries": [dict(entry)]}
                ).entries[0]
                for entry in entries
            ]
        )
        self.item_stream_payload = store.to_payload()

    async def get_user_memory_control(self, *, user_id: int, channel: str | None = None):
        return {"effective_enabled": False}

    async def get_conversation_context_state(self, conversation_id: int):
        return None

    async def get_conversation_compacted_history(self, conversation_id: int):
        return None

    async def get_user_chat_preferences(self, *, user_id: int):
        return {}


class _HistoryRuntime(_RuntimeRecorder):
    def __init__(self):
        super().__init__()
        self.item_stream_payload = {
            "version": "conversation_item_stream.v1",
            "updated_at": "2026-04-02T00:00:00",
            "entries": [
                {
                    "item_id": "item-user",
                    "kind": "user_message",
                    "turn_id": "turn:10",
                    "role": "user",
                    "content": "前一轮问题",
                    "message_id": 10,
                },
                {
                    "item_id": "item-assistant",
                    "kind": "assistant_message",
                    "turn_id": "turn:10",
                    "role": "assistant",
                    "content": "前一轮回答",
                    "message_id": 11,
                },
            ],
        }

    async def get_conversation_item_stream(self, conversation_id: int):
        return dict(self.item_stream_payload) if self.item_stream_payload is not None else None

    async def get_user_memory_control(self, *, user_id: int, channel: str | None = None):
        return {"effective_enabled": False}

    async def get_conversation_context_state(self, conversation_id: int):
        return None

    async def get_conversation_compacted_history(self, conversation_id: int):
        return None

    async def get_conversation_tool_ledger(self, conversation_id: int):
        return None

    async def get_user_chat_preferences(self, *, user_id: int):
        return {}


class _NoConversationArtifactsRuntime(_RuntimeRecorder):
    async def get_user_memory_control(self, *, user_id: int, channel: str | None = None):
        return {"effective_enabled": False}

    async def get_conversation_context_state(self, conversation_id: int):
        raise AssertionError("literature profile should not load chat conversation context state")

    async def get_conversation_compacted_history(self, conversation_id: int):
        raise AssertionError("literature profile should not load chat compacted history")

    async def get_conversation_item_stream(self, conversation_id: int):
        raise AssertionError("literature profile should not load chat item stream")

    async def get_user_chat_preferences(self, *, user_id: int):
        return {}


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


@pytest.mark.asyncio
async def test_literature_profile_skips_chat_conversation_artifact_loading():
    runtime = _NoConversationArtifactsRuntime()
    agent = ReActAgent(
        _SimpleLLM(),
        _BrokenIntentTools(),
        max_iterations=1,
        runtime_context=AgentRuntimeContext(
            user_id=1,
            channel="literature",
            conversation_id=99,
            scope_type="literature_session",
            scope_id="99",
        ),
        runtime_service=runtime,
    )
    context = AgentContext(messages=[{"role": "user", "content": "解释这篇论文"}], max_iterations=1)

    await agent._prepare_runtime_context(context)

    assert context.history_messages == []
    assert context.conversation_state == {}
    assert context.compacted_history == {}


@pytest.mark.asyncio
async def test_prepare_runtime_context_merges_item_stream_history_into_current_messages():
    runtime = _HistoryRuntime()
    agent = ReActAgent(
        _SimpleLLM(),
        _BrokenIntentTools(),
        max_iterations=1,
        runtime_context=AgentRuntimeContext(user_id=1, channel="chat", conversation_id=42),
        runtime_service=runtime,
    )
    context = AgentContext(messages=[{"role": "user", "content": "当前问题"}], max_iterations=1)

    await agent._prepare_runtime_context(context)

    assert [item["content"] for item in context.history_messages] == ["前一轮问题", "前一轮回答"]
    assert [item["content"] for item in context.messages] == ["前一轮问题", "前一轮回答", "当前问题"]


@pytest.mark.asyncio
async def test_prepare_runtime_context_does_not_fallback_without_item_stream():
    runtime = _HistoryRuntime()
    runtime.item_stream_payload = None
    agent = ReActAgent(
        _SimpleLLM(),
        _BrokenIntentTools(),
        max_iterations=1,
        runtime_context=AgentRuntimeContext(user_id=1, channel="chat", conversation_id=42),
        runtime_service=runtime,
    )
    context = AgentContext(messages=[{"role": "user", "content": "当前问题"}], max_iterations=1)

    await agent._prepare_runtime_context(context)

    assert context.history_messages == []
    assert [item["content"] for item in context.messages] == ["当前问题"]


def test_history_messages_from_item_stream_respects_latest_compact_boundary():
    history_messages = ReActAgent._history_messages_from_item_stream(
        [
            {
                "item_id": "item-1",
                "kind": "user_message",
                "role": "user",
                "content": "旧问题",
                "message_id": 10,
            },
            {
                "item_id": "item-2",
                "kind": "assistant_message",
                "role": "assistant",
                "content": "旧回答",
                "message_id": 11,
            },
            {
                "item_id": "item-3",
                "kind": "compact_boundary",
                "role": "system",
                "message_id": 11,
                "metadata": {
                    "compact_boundary_message_id": 11,
                    "replacement_history": [
                        {"role": "system", "content": "此前已经解释旧问题。"},
                    ],
                },
            },
            {
                "item_id": "item-4",
                "kind": "user_message",
                "role": "user",
                "content": "新问题",
                "message_id": 12,
            },
            {
                "item_id": "item-5",
                "kind": "tool_result",
                "role": "tool",
                "tool_name": "knowledge_search",
                "summary": "工具结果",
            },
            {
                "item_id": "item-6",
                "kind": "assistant_message",
                "role": "assistant",
                "content": "新回答",
                "message_id": 13,
            },
        ]
    )

    assert [item["role"] for item in history_messages] == ["system", "user", "assistant"]
    assert [item["content"] for item in history_messages] == ["此前已经解释旧问题。", "新问题", "新回答"]


def test_history_messages_from_item_stream_keeps_current_turn_messages_when_boundary_requests_it():
    history_messages = ReActAgent._history_messages_from_item_stream(
        [
            {
                "item_id": "item-1",
                "kind": "user_message",
                "turn_id": "turn:100",
                "role": "user",
                "content": "旧问题",
                "message_id": 100,
            },
            {
                "item_id": "item-2",
                "kind": "user_message",
                "turn_id": "turn:200",
                "role": "user",
                "content": "当前问题",
                "message_id": 200,
            },
            {
                "item_id": "item-3",
                "kind": "compact_boundary",
                "turn_id": "turn:200",
                "role": "system",
                "message_id": 200,
                "metadata": {
                    "compact_boundary_message_id": 200,
                    "keep_turn_id": "turn:200",
                    "replacement_history": [
                        {"role": "system", "content": "此前已经压缩旧历史。"},
                    ],
                },
            },
            {
                "item_id": "item-4",
                "kind": "tool_result",
                "turn_id": "turn:200",
                "role": "tool",
                "tool_name": "knowledge_search",
                "summary": "工具结果",
            },
        ]
    )

    assert [item["role"] for item in history_messages] == ["system", "user"]
    assert [item["content"] for item in history_messages] == ["此前已经压缩旧历史。", "当前问题"]


def test_history_messages_from_item_stream_reuses_newest_surviving_replacement_checkpoint():
    history_messages = ReActAgent._history_messages_from_item_stream(
        [
            {
                "item_id": "item-1",
                "kind": "compact_boundary",
                "role": "system",
                "message_id": 90,
                "metadata": {
                    "compact_boundary_message_id": 90,
                    "replacement_history": [
                        {"role": "system", "content": "最早的替代历史。"},
                    ],
                },
            },
            {
                "item_id": "item-2",
                "kind": "compact_boundary",
                "role": "system",
                "message_id": 100,
                "metadata": {
                    "compact_boundary_message_id": 100,
                    "replacement_history": [
                        {"role": "assistant", "content": "中间替代历史。"},
                    ],
                },
            },
            {
                "item_id": "item-3",
                "kind": "user_message",
                "turn_id": "turn:200",
                "role": "user",
                "content": "当前问题",
                "message_id": 200,
            },
            {
                "item_id": "item-4",
                "kind": "compact_boundary",
                "turn_id": "turn:200",
                "role": "system",
                "message_id": 200,
                "metadata": {
                    "compact_boundary_message_id": 200,
                    "keep_turn_id": "turn:200",
                },
            },
            {
                "item_id": "item-5",
                "kind": "assistant_message",
                "turn_id": "turn:200",
                "role": "assistant",
                "content": "当前回答",
                "message_id": 201,
            },
        ]
    )

    assert [item["role"] for item in history_messages] == ["assistant", "user", "assistant"]
    assert [item["content"] for item in history_messages] == ["中间替代历史。", "当前问题", "当前回答"]


def test_history_messages_from_item_stream_includes_summary_items_as_thought_rows():
    history_messages = ReActAgent._history_messages_from_item_stream(
        [
            {
                "item_id": "item-1",
                "kind": "reasoning_summary",
                "turn_id": "turn:1",
                "role": "assistant",
                "summary": "先确认问题范围，再收束回答。",
            },
            {
                "item_id": "item-2",
                "kind": "tool_use_summary",
                "turn_id": "turn:1",
                "role": "assistant",
                "summary": "knowledge_search 已执行：找到 Bahdanau 2014 相关资料。",
            },
        ]
    )

    assert len(history_messages) == 2
    assert history_messages[0]["role"] == "assistant"
    assert history_messages[0]["thought"] == "先确认问题范围，再收束回答。"
    assert history_messages[1]["thought"] == "knowledge_search 已执行：找到 Bahdanau 2014 相关资料。"


def test_active_history_messages_from_item_stream_excludes_replacement_history_rows():
    history_messages = ReActAgent._active_history_messages_from_item_stream(
        [
            {
                "item_id": "item-1",
                "kind": "compact_boundary",
                "role": "system",
                "message_id": 11,
                "metadata": {
                    "compact_boundary_message_id": 11,
                    "replacement_history": [
                        {"role": "system", "content": "此前已经解释旧问题。"},
                    ],
                },
            },
            {
                "item_id": "item-2",
                "kind": "user_message",
                "role": "user",
                "content": "新问题",
                "message_id": 12,
            },
            {
                "item_id": "item-3",
                "kind": "assistant_message",
                "role": "assistant",
                "content": "新回答",
                "message_id": 13,
            },
        ]
    )

    assert [item["role"] for item in history_messages] == ["user", "assistant"]
    assert [item["content"] for item in history_messages] == ["新问题", "新回答"]


@pytest.mark.asyncio
async def test_prepare_llm_messages_does_not_duplicate_replacement_history_prefix():
    agent = ReActAgent(_SimpleLLM(), _BrokenIntentTools(), max_iterations=1)
    context = AgentContext(
        messages=[
            {"role": "user", "content": "当前问题"},
        ],
        history_messages=[
            {"role": "user", "content": "当前问题"},
        ],
        compacted_history={
            "version": "conversation_compacted_history.v2",
            "replacement_history": [
                {"role": "system", "content": "此前已经解释旧问题。"},
            ],
        },
    )

    prepared = await agent._prepare_llm_messages(context, system_prompt="system")

    matching = [
        item
        for item in prepared
        if str(item.get("role") or "").lower() == "system"
        and str(item.get("content") or "") == "此前已经解释旧问题。"
    ]
    assert len(matching) == 1


@pytest.mark.asyncio
async def test_mid_run_compaction_appends_boundary_and_refreshes_context(monkeypatch):
    monkeypatch.setattr(settings, "agent_context_window_turns", 1)
    monkeypatch.setattr(settings, "agent_context_recently_slid_turns", 0)
    monkeypatch.setattr(settings, "agent_mid_run_compaction_enabled", True)
    monkeypatch.setattr(settings, "agent_mid_run_compaction_min_iteration", 2)
    monkeypatch.setattr(settings, "agent_mid_run_compaction_max_per_run", 2)
    monkeypatch.setattr(compaction_module, "LLMService", _CompactionLLM)

    runtime = _MidRunRuntime()
    agent = ReActAgent(
        _SimpleLLM(),
        _BrokenIntentTools(),
        max_iterations=2,
        runtime_context=AgentRuntimeContext(user_id=1, channel="chat", conversation_id=42, turn_id="turn:200"),
        runtime_service=runtime,
    )

    run_context = AgentContext(
        messages=[
            {"role": "user", "content": "旧问题"},
            {"role": "assistant", "content": "旧回答"},
            {"role": "user", "content": "当前问题"},
        ],
        turn_id="turn:200",
        iteration=2,
        run_id="run-1",
        context_truncated=True,
    )

    compacted = await agent._maybe_mid_run_compact(run_context, "system")

    assert compacted is True
    assert run_context.mid_run_compactions == 1
    assert runtime.context_states
    assert runtime.compacted_histories
    assert runtime.history_events
    assert runtime.snapshots
    assert run_context.compacted_history["mid_run"] is True
    boundary_entry = runtime.item_stream_payload["entries"][-1]
    assert boundary_entry["kind"] == "compact_boundary"
    assert boundary_entry["metadata"]["keep_turn_id"] == "turn:200"
    assert [item["role"] for item in run_context.history_messages] == ["user"]
    assert [item["content"] for item in run_context.history_messages] == ["当前问题"]
    assert run_context.compacted_history["replacement_history"][0]["content"] == "此前已经解释旧问题。"
    assert [item["role"] for item in run_context.messages] == ["user"]
    assert [item["content"] for item in run_context.messages] == ["当前问题"]


@pytest.mark.asyncio
async def test_mid_run_compaction_can_trigger_on_message_pressure_without_trim(monkeypatch):
    monkeypatch.setattr(settings, "agent_context_window_turns", 1)
    monkeypatch.setattr(settings, "agent_context_recently_slid_turns", 0)
    monkeypatch.setattr(settings, "agent_mid_run_compaction_enabled", True)
    monkeypatch.setattr(settings, "agent_mid_run_compaction_min_iteration", 2)
    monkeypatch.setattr(settings, "agent_mid_run_compaction_max_per_run", 2)
    monkeypatch.setattr(settings, "agent_mid_run_compaction_message_tokens_trigger", 256)
    monkeypatch.setattr(compaction_module, "LLMService", _CompactionLLM)

    runtime = _MidRunRuntime()
    agent = ReActAgent(
        _SimpleLLM(),
        _BrokenIntentTools(),
        max_iterations=2,
        runtime_context=AgentRuntimeContext(user_id=1, channel="chat", conversation_id=42, turn_id="turn:200"),
        runtime_service=runtime,
    )

    run_context = AgentContext(
        messages=[
            {"role": "user", "content": "旧问题"},
            {"role": "assistant", "content": "旧回答"},
            {"role": "user", "content": "当前问题"},
        ],
        turn_id="turn:200",
        iteration=2,
        run_id="run-1",
        context_truncated=False,
        message_tokens_before_trim=512,
    )

    compacted = await agent._maybe_mid_run_compact(run_context, "system")

    assert compacted is True
    assert run_context.mid_run_compactions == 1
    assert runtime.compacted_histories


@pytest.mark.asyncio
async def test_pre_turn_compaction_persists_boundary_and_refreshes_context(monkeypatch):
    monkeypatch.setattr(settings, "agent_pre_turn_compaction_enabled", True)
    monkeypatch.setattr(settings, "agent_context_window_turns", 1)
    monkeypatch.setattr(settings, "agent_context_recently_slid_turns", 0)
    monkeypatch.setattr(compaction_module, "LLMService", _CompactionLLM)

    runtime = _MidRunRuntime()
    agent = ReActAgent(
        _SimpleLLM(),
        _BrokenIntentTools(),
        max_iterations=1,
        runtime_context=AgentRuntimeContext(user_id=1, channel="chat", conversation_id=42, turn_id="turn:200"),
        runtime_service=runtime,
    )
    context = AgentContext(
        messages=[{"role": "user", "content": "当前问题"}],
        turn_id="turn:200",
        iteration=0,
        run_id="run-1",
    )

    compacted = await agent._maybe_pre_turn_compact(context)

    assert compacted is True
    assert runtime.context_states
    assert runtime.compacted_histories
    assert runtime.history_events[-1][1] == "pre_turn_compact"
    boundary_entry = runtime.item_stream_payload["entries"][-1]
    assert boundary_entry["kind"] == "compact_boundary"
    assert boundary_entry["status"] == "pre_turn"
    assert boundary_entry["metadata"]["keep_turn_id"] == "turn:200"
    assert context.compacted_history["mode"] == "pre_turn"
    assert [item["role"] for item in context.history_messages] == ["user"]
    assert [item["content"] for item in context.history_messages] == ["当前问题"]
    assert context.context_debug["formal_compaction_applied"] is True
    assert context.context_debug["formal_compaction_mode"] == "pre_turn"


@pytest.mark.asyncio
async def test_run_emits_pre_turn_compaction_thought(monkeypatch):
    monkeypatch.setattr(settings, "agent_pre_turn_compaction_enabled", True)
    monkeypatch.setattr(settings, "agent_context_window_turns", 1)
    monkeypatch.setattr(settings, "agent_context_recently_slid_turns", 0)
    monkeypatch.setattr(compaction_module, "LLMService", _CompactionLLM)

    runtime = _MidRunRuntime()
    agent = ReActAgent(
        _SimpleLLM(),
        _BrokenIntentTools(),
        max_iterations=1,
        runtime_context=AgentRuntimeContext(user_id=1, channel="chat", conversation_id=42, turn_id="turn:200"),
        runtime_service=runtime,
    )

    events = []
    async for event in agent.run([{"role": "user", "content": "当前问题"}], stream=False):
        events.append(event)

    thought_messages = [str(event.get("data") or "") for event in events if event.get("type") == "thought"]
    assert any("发送前已压缩较早上下文" in message for message in thought_messages)
    assert any(event.get("type") == "done" for event in events)
