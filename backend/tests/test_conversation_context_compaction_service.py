import json
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app.services.conversation_context_compaction_service as compaction_module
from app.config import settings
from app.models.conversation import Conversation
from app.services.conversation_context_compaction_service import ConversationContextCompactionService


class _FakeStateLLM:
    provider = "test"
    config = {"model": "fake-context-state-model"}
    last_messages = None
    last_system_prompt = None
    calls = []

    def __init__(self, provider=None):
        if provider:
            self.provider = provider

    async def chat(self, messages, system_prompt=None, **kwargs):
        _FakeStateLLM.last_messages = list(messages)
        _FakeStateLLM.last_system_prompt = system_prompt
        payload = None
        if messages:
            try:
                payload = json.loads(str(messages[0].get("content") or ""))
            except Exception:
                payload = None
        _FakeStateLLM.calls.append(
            {
                "messages": list(messages),
                "system_prompt": system_prompt,
                "payload": payload,
            }
        )
        if "会话历史压缩器" in str(system_prompt or ""):
            return {
                "content": json.dumps(
                    {
                        "history_anchors": "开场目标: 解释注意力机制；仍有效约束: 用中文回答，不要联网。",
                        "history_summary": "用户先要求解释注意力机制，随后追问创新点与历史背景，当前仍围绕注意力机制的核心原理与历史条件展开。",
                        "replacement_history": [
                            {"role": "system", "content": "本轮继续围绕注意力机制的历史条件展开。"},
                            {"role": "assistant", "content": "此前已解释注意力机制定义与创新点。"},
                        ],
                    },
                    ensure_ascii=False,
                )
            }
        return {
            "content": json.dumps(
                {
                    "active_topic": "注意力机制",
                    "user_goal": "解释注意力机制为什么以前没有被提出",
                    "constraints": ["用中文回答", "不要联网"],
                    "open_questions": ["为什么以前没发现"],
                    "resolved_facts": ["Bahdanau 2014 是关键时间点"],
                    "evidence_ledger": [
                        {
                            "summary": "已检索 attention mechanism 定义",
                            "status": "confirmed",
                            "source_labels": ["来源1", "来源2"],
                            "tool_names": ["knowledge_search"],
                        }
                    ],
                    "last_reasoning_summary": "先解释定义，再回答历史条件限制。",
                },
                ensure_ascii=False,
            )
        }


@pytest.mark.asyncio
async def test_build_artifacts_uses_llm_to_extract_context_state(monkeypatch):
    monkeypatch.setattr(settings, "agent_context_window_turns", 2)
    monkeypatch.setattr(settings, "agent_context_recently_slid_turns", 1)
    monkeypatch.setattr(settings, "agent_context_state_provider", "aliyun")
    monkeypatch.setattr(settings, "agent_context_state_model", "qwen3.5-flash")
    monkeypatch.setattr(compaction_module, "LLMService", _FakeStateLLM)
    _FakeStateLLM.calls = []

    artifacts = await ConversationContextCompactionService.build_artifacts(
        [
            {"role": "user", "content": "解释注意力机制。"},
            {
                "role": "assistant",
                "content": "第一轮回答。",
                "metadata": {
                    "reasoning_summary": {
                        "summary": "这段旧 metadata 不应该再进入 compaction 预览。"
                    }
                },
            },
            {"role": "user", "content": "创新点在哪？"},
            {"role": "assistant", "content": "第二轮回答。"},
            {"role": "user", "content": "为什么以前没发现？"},
        ],
        tool_ledger_entries=[
            {
                "entry_id": "tool-call-1",
                "kind": "tool_call",
                "tool_name": "knowledge_search",
                "tool_call_id": "call_1",
                "iteration": 1,
                "status": "started",
                "arguments": {"query": "attention mechanism 定义"},
            },
            {
                "entry_id": "tool-result-1",
                "kind": "tool_result",
                "tool_name": "knowledge_search",
                "tool_call_id": "call_1",
                "iteration": 1,
                "status": "succeeded",
                "summary": "检索到 attention mechanism 定义以及 Bahdanau 2014 相关资料。",
                "success": True,
                "metadata": {
                    "source_kind": "knowledge_base_search",
                    "source_labels": ["来源1", "来源2"],
                    "result_count": 2,
                    "retrieval_scope": {"knowledge_base_ids": [12], "document_ids": [34]},
                    "evidence_preview": [
                        {
                            "knowledge_base": "Transformer",
                            "document": "Attention Is All You Need.pdf",
                            "citation_label": "Attention Is All You Need.pdf · chunk 3",
                        }
                    ],
                },
            },
        ],
    )

    assert artifacts.context_state["version"] == "conversation_context_state.v3"
    assert artifacts.context_state["active_topic"] == "注意力机制"
    assert artifacts.context_state["user_goal"] == "解释注意力机制为什么以前没有被提出"
    assert "为什么以前没发现" in artifacts.context_state["open_questions"]
    assert "Bahdanau 2014 是关键时间点" in artifacts.context_state["resolved_facts"]
    assert artifacts.context_state["evidence_ledger"][0]["entry_id"].startswith("evidence:")
    assert artifacts.context_state["evidence_ledger"][0]["origin_kind"] == "tool_result"
    assert artifacts.context_state["evidence_ledger"][0]["tool_names"] == ["knowledge_search"]
    assert artifacts.context_state["evidence_ledger"][0]["source_kind"] == "knowledge_base_search"
    assert artifacts.context_state["evidence_ledger"][0]["result_count"] == 2
    assert artifacts.context_state["evidence_ledger"][0]["retrieval_scope"] == {"knowledge_base_ids": [12], "document_ids": [34]}
    assert artifacts.context_state["evidence_ledger"][0]["provenance_hints"] == ["Transformer / Attention Is All You Need.pdf"]
    assert artifacts.compacted_history["version"] == "conversation_compacted_history.v2"
    assert "开场目标" in artifacts.compacted_history["history_anchors"]
    assert "注意力机制" in artifacts.compacted_history["history_summary"]
    assert len(artifacts.compacted_history["replacement_history"]) == 2
    assert artifacts.compacted_message_count > 0
    assert "注意力机制" in artifacts.summary_text
    assert _FakeStateLLM.last_messages
    assert "严格 JSON" in str(_FakeStateLLM.last_system_prompt or "")
    assert len(_FakeStateLLM.calls) == 2
    recent_messages_preview = _FakeStateLLM.calls[0]["payload"]["recent_messages"]
    assert all("reasoning_summary" not in row for row in recent_messages_preview)
    assert _FakeStateLLM.calls[0]["payload"]["tool_ledger_preview"][0]["tool_name"] == "knowledge_search"
    assert _FakeStateLLM.calls[0]["payload"]["tool_ledger_preview"][0]["summary"].startswith("检索到")
    assert _FakeStateLLM.calls[0]["payload"]["tool_ledger_preview"][0]["source_kind"] == "knowledge_base_search"
    assert _FakeStateLLM.calls[0]["payload"]["evidence_candidates"][0]["tool_names"] == ["knowledge_search"]
    assert "检索到 attention mechanism 定义" in _FakeStateLLM.calls[0]["payload"]["evidence_candidates"][0]["summary"]
    assert _FakeStateLLM.calls[0]["payload"]["evidence_candidates"][0]["source_kind"] == "knowledge_base_search"
    assert _FakeStateLLM.calls[1]["payload"]["tool_ledger_preview"][0]["tool_name"] == "knowledge_search"

def test_require_item_stream_payload_raises_on_missing_entries():
    with pytest.raises(compaction_module.ConversationItemStreamUnavailableError) as exc_info:
        ConversationContextCompactionService._require_item_stream_payload(42, None)

    assert exc_info.value.conversation_id == 42


@pytest.mark.asyncio
async def test_build_artifacts_merges_duplicate_evidence_with_tool_candidate(monkeypatch):
    monkeypatch.setattr(settings, "agent_context_window_turns", 2)
    monkeypatch.setattr(settings, "agent_context_recently_slid_turns", 1)
    monkeypatch.setattr(compaction_module, "LLMService", _FakeSparseEvidenceLLM)

    artifacts = await ConversationContextCompactionService.build_artifacts(
        [
            {"role": "user", "content": "解释注意力机制。"},
            {"role": "assistant", "content": "先解释定义。"},
            {"role": "user", "content": "再补一条可靠证据。"},
        ],
        tool_ledger_entries=[
            {
                "entry_id": "tool-result-1",
                "kind": "tool_result",
                "tool_name": "knowledge_search",
                "tool_call_id": "call_1",
                "iteration": 1,
                "status": "succeeded",
                "summary": "检索到 attention mechanism 定义以及 Bahdanau 2014 相关资料。",
                "success": True,
            },
        ],
    )

    assert artifacts.context_state["evidence_ledger"]
    evidence = artifacts.context_state["evidence_ledger"][0]
    assert evidence["entry_id"].startswith("evidence:")
    assert evidence["origin_kind"] == "tool_result"
    assert evidence["summary"] == "检索到 attention mechanism 定义以及 Bahdanau 2014 相关资料。"
    assert evidence["status"] == "confirmed"
    assert evidence["tool_names"] == ["knowledge_search"]
    assert evidence["turn_ids"] == []
    assert evidence["tool_call_ids"] == ["call_1"]
    assert (
        "检索到 attention mechanism 定义以及 Bahdanau 2014 相关资料。"
        in artifacts.context_state["resolved_facts"]
    )


@pytest.mark.asyncio
async def test_build_artifacts_preserves_evidence_provenance_when_merging_candidates(monkeypatch):
    monkeypatch.setattr(settings, "agent_context_window_turns", 2)
    monkeypatch.setattr(settings, "agent_context_recently_slid_turns", 1)
    monkeypatch.setattr(compaction_module, "LLMService", _FakeSparseEvidenceLLM)

    artifacts = await ConversationContextCompactionService.build_artifacts(
        [
            {"role": "user", "content": "继续补一条事实。"},
            {"role": "assistant", "content": "好的。"},
        ],
        tool_ledger_entries=[
            {
                "entry_id": "tool-result-2",
                "kind": "tool_result",
                "tool_name": "knowledge_search",
                "turn_id": "turn:42",
                "tool_call_id": "call_42",
                "iteration": 2,
                "status": "succeeded",
                "summary": "检索到 attention mechanism 定义以及 Bahdanau 2014 相关资料。",
                "success": True,
                "metadata": {
                    "source_kind": "knowledge_base_search",
                    "source_labels": ["来源7"],
                    "result_count": 1,
                    "retrieval_scope": {"knowledge_base_ids": [7], "document_ids": []},
                },
            },
        ],
    )

    evidence = artifacts.context_state["evidence_ledger"][0]
    assert evidence["entry_id"].startswith("evidence:")
    assert evidence["origin_kind"] == "tool_result"
    assert evidence["tool_names"] == ["knowledge_search"]
    assert evidence["turn_ids"] == ["turn:42"]
    assert evidence["tool_call_ids"] == ["call_42"]
    assert evidence["source_kind"] == "knowledge_base_search"
    assert evidence["source_labels"] == ["来源7"]
    assert evidence["result_count"] == 1


@pytest.mark.asyncio
async def test_build_artifacts_force_compact_keeps_manual_compact_effective_before_window_slides(monkeypatch):
    monkeypatch.setattr(settings, "agent_context_window_turns", 8)
    monkeypatch.setattr(settings, "agent_context_recently_slid_turns", 2)
    monkeypatch.setattr(compaction_module, "LLMService", _FakeStateLLM)

    messages = [
        {"role": "user", "content": "第1轮问题。"},
        {"role": "assistant", "content": "第1轮回答。"},
        {"role": "user", "content": "第2轮问题。"},
        {"role": "assistant", "content": "第2轮回答。"},
        {"role": "user", "content": "第3轮问题。"},
        {"role": "assistant", "content": "第3轮回答。"},
        {"role": "user", "content": "第4轮问题。"},
        {"role": "assistant", "content": "第4轮回答。"},
        {"role": "user", "content": "第5轮问题。"},
        {"role": "assistant", "content": "第5轮回答。"},
        {"role": "user", "content": "第6轮问题。"},
        {"role": "assistant", "content": "第6轮回答。"},
    ]

    normal_artifacts = await ConversationContextCompactionService.build_artifacts(messages)
    forced_artifacts = await ConversationContextCompactionService.build_artifacts(
        messages,
        force_compact=True,
    )

    assert normal_artifacts.compacted_history == {}
    assert normal_artifacts.compacted_message_count == 0
    assert forced_artifacts.compacted_history["replacement_history"]
    assert forced_artifacts.compacted_message_count > 0


class _FakeNoCompactLLM:
    provider = "test"
    config = {"model": "fake-no-compact"}

    def __init__(self, provider=None):
        if provider:
            self.provider = provider

    async def chat(self, messages, system_prompt=None, **kwargs):
        if "会话历史压缩器" in str(system_prompt or ""):
            return {"content": "{}"}
        return {
            "content": json.dumps(
                {
                    "active_topic": "注意力机制",
                    "user_goal": "解释注意力机制",
                    "constraints": [],
                    "open_questions": [],
                    "resolved_facts": [],
                    "evidence_ledger": [],
                    "last_reasoning_summary": "概念解释",
                },
                ensure_ascii=False,
            )
        }


class _FakeSparseEvidenceLLM:
    provider = "test"
    config = {"model": "fake-sparse-evidence"}

    def __init__(self, provider=None):
        if provider:
            self.provider = provider

    async def chat(self, messages, system_prompt=None, **kwargs):
        if "会话历史压缩器" in str(system_prompt or ""):
            return {
                "content": json.dumps(
                    {
                        "history_anchors": "历史锚点",
                        "history_summary": "历史摘要",
                        "replacement_history": [],
                    },
                    ensure_ascii=False,
                )
            }
        return {
            "content": json.dumps(
                {
                    "active_topic": "注意力机制",
                    "user_goal": "补充外部证据",
                    "constraints": [],
                    "open_questions": [],
                    "resolved_facts": [],
                    "evidence_ledger": [
                        {
                            "summary": "检索到 attention mechanism 定义以及 Bahdanau 2014 相关资料。",
                            "status": "provisional",
                            "source_labels": [],
                            "tool_names": [],
                        }
                    ],
                    "last_reasoning_summary": "已有外部证据。",
                },
                ensure_ascii=False,
            )
        }


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self, conversation):
        self._conversation = conversation

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt):
        return _ScalarResult(self._conversation)


class _FakeRuntimeService:
    def __init__(self, item_stream_payload, *, context_state=None, compacted_history=None):
        self.item_stream_payload = item_stream_payload
        self.item_entries = []
        self.history_events = []
        self.snapshots = []
        self.context_state = dict(context_state) if isinstance(context_state, dict) else None
        self.compacted_history = dict(compacted_history) if isinstance(compacted_history, dict) else None

    async def get_conversation_item_stream(self, conversation_id: int):
        return self.item_stream_payload

    async def get_conversation_context_state(self, conversation_id: int):
        return dict(self.context_state) if isinstance(self.context_state, dict) else None

    async def get_conversation_compacted_history(self, conversation_id: int):
        return dict(self.compacted_history) if isinstance(self.compacted_history, dict) else None

    async def upsert_conversation_context_state(self, conversation_id: int, payload):
        self.context_state = dict(payload)

    async def upsert_conversation_compacted_history(self, conversation_id: int, payload):
        self.compacted_history = dict(payload)

    async def append_conversation_history_event(self, conversation_id: int, *, title: str, detail: str):
        self.history_events.append({"title": title, "detail": detail})

    async def append_conversation_context_snapshot(self, conversation_id: int, snapshot):
        self.snapshots.append(dict(snapshot))

    async def append_conversation_item_entries(self, conversation_id: int, entries):
        self.item_entries.extend(list(entries or []))


@pytest.mark.asyncio
async def test_compact_conversation_does_not_append_empty_boundary(monkeypatch):
    monkeypatch.setattr(compaction_module, "LLMService", _FakeNoCompactLLM)
    conversation = Conversation(
        id=77,
        user_id=5,
        title="测试",
        llm_provider="aliyun",
        is_archived=0,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    runtime_service = _FakeRuntimeService(
        {
            "version": "conversation_item_stream.v1",
            "entries": [
                {
                    "item_id": "user-1",
                    "kind": "user_message",
                    "turn_id": "turn:1",
                    "role": "user",
                    "content": "解释注意力机制。",
                    "message_id": 10,
                    "created_at": datetime.utcnow().isoformat(),
                },
                {
                    "item_id": "assistant-1",
                    "kind": "assistant_message",
                    "turn_id": "turn:1",
                    "role": "assistant",
                    "content": "注意力机制是一种动态聚焦机制。",
                    "message_id": 11,
                    "created_at": datetime.utcnow().isoformat(),
                },
            ],
        }
    )
    service = ConversationContextCompactionService()
    service._runtime_service = runtime_service
    monkeypatch.setattr(compaction_module, "async_session_factory", lambda: _FakeSession(conversation))

    artifacts = await service.compact_now(77)

    assert artifacts.context_state["active_topic"] == "注意力机制"
    assert artifacts.compacted_history == {}
    assert runtime_service.context_state is not None
    assert runtime_service.compacted_history is None
    assert runtime_service.item_entries == []


@pytest.mark.asyncio
async def test_compact_conversation_skips_when_boundary_already_covers_latest_message(monkeypatch):
    monkeypatch.setattr(compaction_module, "LLMService", _FakeStateLLM)
    conversation = Conversation(
        id=88,
        user_id=5,
        title="测试",
        llm_provider="aliyun",
        is_archived=0,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    runtime_service = _FakeRuntimeService(
        {
            "version": "conversation_item_stream.v1",
            "entries": [
                {
                    "item_id": "user-1",
                    "kind": "user_message",
                    "turn_id": "turn:1",
                    "role": "user",
                    "content": "解释注意力机制。",
                    "message_id": 10,
                    "created_at": datetime.utcnow().isoformat(),
                },
                {
                    "item_id": "assistant-1",
                    "kind": "assistant_message",
                    "turn_id": "turn:1",
                    "role": "assistant",
                    "content": "注意力机制是一种动态聚焦机制。",
                    "message_id": 11,
                    "created_at": datetime.utcnow().isoformat(),
                },
                {
                    "item_id": "boundary-1",
                    "kind": "compact_boundary",
                    "role": "system",
                    "message_id": 11,
                    "metadata": {
                        "compact_boundary_message_id": 11,
                        "replacement_history": [
                            {"role": "system", "content": "此前已经压缩旧历史。"},
                        ],
                    },
                    "created_at": datetime.utcnow().isoformat(),
                },
            ],
        },
        context_state={
            "version": "conversation_context_state.v3",
            "active_topic": "注意力机制",
            "user_goal": "解释原理",
            "constraints": [],
            "open_questions": [],
            "resolved_facts": [],
            "evidence_ledger": [],
            "last_reasoning_summary": "已有摘要",
            "turn_count": 1,
            "updated_at": datetime.utcnow().isoformat(),
        },
        compacted_history={
            "version": "conversation_compacted_history.v2",
            "history_anchors": "已压缩历史",
            "history_summary": "此前已经压缩旧历史。",
            "compact_boundary_message_id": 11,
            "replacement_history": [
                {"role": "system", "content": "此前已经压缩旧历史。"},
            ],
            "compacted_message_count": 2,
            "up_to_message_id": 11,
            "updated_at": datetime.utcnow().isoformat(),
        },
    )
    service = ConversationContextCompactionService()
    service._runtime_service = runtime_service
    monkeypatch.setattr(compaction_module, "async_session_factory", lambda: _FakeSession(conversation))

    _FakeStateLLM.calls = []
    artifacts = await service.compact_now(88)

    assert artifacts.compacted_history["compact_boundary_message_id"] == 11
    assert artifacts.summary_text == "此前已经压缩旧历史。"
    assert artifacts.compacted_message_count == 0
    assert runtime_service.history_events == []
    assert runtime_service.snapshots == []
    assert runtime_service.item_entries == []
    assert _FakeStateLLM.calls == []
