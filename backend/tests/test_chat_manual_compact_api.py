import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api import chat as chat_api
from app.models.conversation import Conversation, Message, MessageRole, MessageType
from app.services.conversation_context_compaction_service import (
    ConversationCompactionArtifacts,
    ConversationItemStreamUnavailableError,
)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        return self._value


class _FakeDB:
    def __init__(self, conversation):
        self._conversation = conversation

    async def execute(self, _stmt):
        stmt_text = str(_stmt)
        if "count(messages.id)" in stmt_text.lower():
            return _ScalarResult(len(list(getattr(self._conversation, "messages", []) or [])))
        return _ScalarResult(self._conversation)


class _FakeCompactionService:
    async def compact_now(self, conversation_id: int) -> ConversationCompactionArtifacts:
        assert conversation_id == 42
        return ConversationCompactionArtifacts(
            context_state={
                "version": "conversation_context_state.v3",
                "active_topic": "注意力机制",
                "user_goal": "解释为什么以前没有提出",
                "constraints": [],
                "open_questions": ["为什么以前没发现"],
                "resolved_facts": ["2014 年 Bahdanau 提出注意力"],
                "evidence_ledger": [
                    {
                        "summary": "2014 年 Bahdanau 提出注意力",
                        "status": "confirmed",
                        "source_labels": ["来源1"],
                        "tool_names": ["knowledge_search"],
                    }
                ],
                "last_reasoning_summary": "采用时间线分析",
                "turn_count": 5,
                "updated_at": "2026-03-31T10:00:00",
            },
            compacted_history={
                "version": "conversation_compacted_history.v2",
                "history_anchors": "开场目标：解释注意力机制。",
                "history_summary": "用户先问概念，再追问创新点与为何以前没发现。",
                "compact_boundary_message_id": 99,
                "replacement_history": [
                    {"role": "system", "content": "本轮继续围绕注意力机制展开。"},
                    {"role": "assistant", "content": "此前已解释定义与创新点。"},
                ],
                "compacted_message_count": 4,
                "up_to_message_id": 99,
                "updated_at": "2026-03-31T10:00:00",
            },
            summary_text="用户先问概念，再追问创新点与为何以前没发现。",
            up_to_message_id=99,
            message_count=8,
            compacted_message_count=4,
        )


class _FakeRuntimeService:
    async def get_conversation_context_state(self, conversation_id: int):
        assert conversation_id == 42
        return {
            "version": "conversation_context_state.v3",
            "active_topic": "注意力机制",
            "user_goal": "解释为什么以前没有提出",
            "constraints": [],
            "open_questions": ["为什么以前没发现"],
            "resolved_facts": ["2014 年 Bahdanau 提出注意力"],
            "evidence_ledger": [
                {
                    "summary": "2014 年 Bahdanau 提出注意力",
                    "status": "confirmed",
                    "source_labels": ["来源1"],
                    "tool_names": ["knowledge_search"],
                }
            ],
            "last_reasoning_summary": "采用时间线分析",
            "turn_count": 5,
            "updated_at": "2026-03-31T10:00:00",
        }

    async def get_conversation_compacted_history(self, conversation_id: int):
        assert conversation_id == 42
        return {
            "version": "conversation_compacted_history.v2",
            "history_anchors": "开场目标：解释注意力机制。",
            "history_summary": "用户先问概念，再追问创新点与为何以前没发现。",
            "compact_boundary_message_id": 99,
            "replacement_history": [
                {"role": "system", "content": "本轮继续围绕注意力机制展开。"},
                {"role": "assistant", "content": "此前已解释定义与创新点。"},
            ],
            "compacted_message_count": 4,
            "up_to_message_id": 99,
            "updated_at": "2026-03-31T10:00:00",
        }

    async def get_conversation_history_log(self, conversation_id: int):
        assert conversation_id == 42
        return {
            "version": "conversation_history_log.v1",
            "updated_at": "2026-03-31T10:00:00",
            "events": [
                {
                    "title": "manual_compact",
                    "detail": "compacted_messages=4, summary_chars=22, up_to_message_id=99",
                    "created_at": "2026-03-31T10:00:00",
                }
            ],
        }

    async def get_conversation_tool_ledger(self, conversation_id: int):
        assert conversation_id == 42
        return {
            "version": "conversation_tool_ledger.v1",
            "updated_at": "2026-03-31T10:00:00",
            "entries": [
                {
                    "entry_id": "tool-call-1",
                    "kind": "tool_call",
                    "tool_name": "knowledge_search",
                    "turn_id": "turn:99",
                    "tool_call_id": "call_1",
                    "iteration": 1,
                    "status": "started",
                    "arguments": {"query": "attention mechanism"},
                },
                {
                    "entry_id": "tool-result-1",
                    "kind": "tool_result",
                    "tool_name": "knowledge_search",
                    "turn_id": "turn:99",
                    "tool_call_id": "call_1",
                    "iteration": 1,
                    "status": "succeeded",
                    "summary": "检索到 Bahdanau 2014 相关资料。",
                    "success": True,
                },
            ],
        }

    async def get_conversation_turn_store(self, conversation_id: int):
        assert conversation_id == 42
        return {
            "version": "conversation_turn_store.v1",
            "updated_at": "2026-03-31T10:00:00",
            "entries": [
                {
                    "turn_id": "turn:99",
                    "status": "completed",
                    "user_message_id": 99,
                    "assistant_message_id": 100,
                    "assistant_summary": "此前已解释定义与创新点。",
                    "iteration_count": 2,
                    "tool_call_count": 1,
                    "tool_result_count": 1,
                    "started_at": "2026-03-31T09:58:00",
                    "completed_at": "2026-03-31T09:58:12",
                }
            ],
        }

    async def get_conversation_context_snapshots(self, conversation_id: int):
        assert conversation_id == 42
        return [
            {
                "version": "conversation_context_snapshot.v1",
                "mode": "manual",
                "created_at": "2026-03-31T10:00:00",
                "summary_text": "用户先问概念，再追问创新点与为何以前没发现。",
                "compacted_message_count": 4,
                "up_to_message_id": 99,
                "context_state": {
                    "version": "conversation_context_state.v3",
                    "active_topic": "注意力机制",
                    "user_goal": "解释为什么以前没有提出",
                    "constraints": [],
                    "open_questions": ["为什么以前没发现"],
                    "resolved_facts": ["2014 年 Bahdanau 提出注意力"],
                    "evidence_ledger": [
                        {
                            "summary": "2014 年 Bahdanau 提出注意力",
                            "status": "confirmed",
                            "source_labels": ["来源1"],
                            "tool_names": ["knowledge_search"],
                        }
                    ],
                    "last_reasoning_summary": "采用时间线分析",
                    "turn_count": 5,
                    "updated_at": "2026-03-31T10:00:00",
                },
                "compacted_history": {
                    "version": "conversation_compacted_history.v2",
                    "history_anchors": "开场目标：解释注意力机制。",
                    "history_summary": "用户先问概念，再追问创新点与为何以前没发现。",
                    "compact_boundary_message_id": 99,
                    "replacement_history": [
                        {"role": "system", "content": "本轮继续围绕注意力机制展开。"},
                        {"role": "assistant", "content": "此前已解释定义与创新点。"},
                    ],
                    "compacted_message_count": 4,
                    "up_to_message_id": 99,
                    "updated_at": "2026-03-31T10:00:00",
                },
            }
        ]

    async def get_conversation_item_stream(self, conversation_id: int):
        assert conversation_id == 42
        return {
            "version": "conversation_item_stream.v1",
            "updated_at": "2026-03-31T10:00:00",
            "entries": [
                {
                    "item_id": "item-1",
                    "kind": "user_message",
                    "turn_id": "turn:99",
                    "role": "user",
                    "content": "解释注意力机制。",
                    "message_id": 99,
                    "created_at": "2026-03-31T09:58:00",
                },
                {
                    "item_id": "item-2",
                    "kind": "tool_result",
                    "turn_id": "turn:99",
                    "role": "tool",
                    "tool_name": "knowledge_search",
                    "tool_call_id": "call_1",
                    "iteration": 1,
                    "status": "succeeded",
                    "summary": "检索到 Bahdanau 2014 相关资料。",
                    "success": True,
                    "created_at": "2026-03-31T09:58:12",
                },
            ],
        }


class _User:
    id = 7


def test_project_messages_from_item_stream_respects_canonical_history():
    projected = chat_api._project_messages_from_item_stream(
        conversation_id=42,
        item_stream_payload={
            "version": "conversation_item_stream.v1",
            "entries": [
                {
                    "item_id": "item-1",
                    "kind": "user_message",
                    "role": "user",
                    "content": "旧问题",
                    "message_id": 10,
                    "created_at": "2026-03-31T09:58:00",
                },
                {
                    "item_id": "item-2",
                    "kind": "assistant_message",
                    "role": "assistant",
                    "content": "旧回答",
                    "message_id": 11,
                    "created_at": "2026-03-31T09:58:01",
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
                    "created_at": "2026-03-31T09:58:02",
                },
                {
                    "item_id": "item-4",
                    "kind": "user_message",
                    "role": "user",
                    "content": "新问题",
                    "message_id": 12,
                    "created_at": "2026-03-31T09:58:03",
                },
                {
                    "item_id": "item-5",
                    "kind": "assistant_message",
                    "role": "assistant",
                    "content": "新回答",
                    "message_id": 13,
                    "created_at": "2026-03-31T09:58:04",
                },
            ],
        },
    )

    assert [message.content for message in projected] == ["新问题", "新回答"]


@pytest.mark.asyncio
async def test_compact_conversation_returns_latest_state_and_compacted_history(monkeypatch):
    conversation = Conversation(
        id=42,
        user_id=7,
        title="测试对话",
        llm_provider="aliyun",
        is_archived=0,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    monkeypatch.setattr(chat_api, "get_conversation_context_compaction_service", lambda: _FakeCompactionService())
    monkeypatch.setattr(chat_api, "get_agent_runtime_service", lambda: _FakeRuntimeService())

    response = await chat_api.compact_conversation(
        42,
        current_user=_User(),
        db=_FakeDB(conversation),
    )

    assert response.conversation_id == 42
    assert response.context_state is not None
    assert response.context_state.active_topic == "注意力机制"
    assert response.context_state.resolved_facts == ["2014 年 Bahdanau 提出注意力"]
    assert response.context_state.evidence_ledger[0].summary == "2014 年 Bahdanau 提出注意力"
    assert response.compacted_history is not None
    assert response.compacted_history.history_anchors == "开场目标：解释注意力机制。"
    assert response.compacted_history.compact_boundary_message_id == 99
    assert response.compacted_history.replacement_history[0].role == "system"
    assert response.history_log is not None
    assert response.history_log.events[0].title == "manual_compact"
    assert response.turn_store is not None
    assert response.turn_store.entries[0].turn_id == "turn:99"
    assert response.tool_ledger is not None
    assert response.tool_ledger.entries[0].turn_id == "turn:99"
    assert response.item_stream is not None
    assert response.item_stream.entries[0].turn_id == "turn:99"
    assert response.context_snapshots
    assert response.context_snapshots[0].mode == "manual"
    assert response.summary_text == "用户先问概念，再追问创新点与为何以前没发现。"
    assert response.compacted_message_count == 4


@pytest.mark.asyncio
async def test_compact_conversation_returns_structured_error_when_item_stream_missing(monkeypatch):
    conversation = Conversation(
        id=42,
        user_id=7,
        title="测试对话",
        llm_provider="aliyun",
        is_archived=0,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )

    class _MissingItemStreamCompactionService:
        async def compact_now(self, conversation_id: int):
            raise ConversationItemStreamUnavailableError(conversation_id)

    monkeypatch.setattr(
        chat_api,
        "get_conversation_context_compaction_service",
        lambda: _MissingItemStreamCompactionService(),
    )

    with pytest.raises(chat_api.HTTPException) as exc_info:
        await chat_api.compact_conversation(
            42,
            current_user=_User(),
            db=_FakeDB(conversation),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "conversation_item_stream_missing"
    assert exc_info.value.detail["conversation_id"] == 42


@pytest.mark.asyncio
async def test_compact_conversation_sanitizes_legacy_empty_snapshot_payloads(monkeypatch):
    conversation = Conversation(
        id=42,
        user_id=7,
        title="测试对话",
        llm_provider="aliyun",
        is_archived=0,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )

    class _LegacyRuntimeService(_FakeRuntimeService):
        async def get_conversation_context_snapshots(self, conversation_id: int):
            assert conversation_id == 42
            return [
                {
                    "version": "conversation_context_snapshot.v1",
                    "mode": "manual",
                    "created_at": "2026-03-31T10:00:00",
                    "summary_text": "legacy",
                    "compacted_message_count": 2,
                    "up_to_message_id": 12,
                    "context_state": {},
                    "compacted_history": {},
                }
            ]

    monkeypatch.setattr(chat_api, "get_conversation_context_compaction_service", lambda: _FakeCompactionService())
    monkeypatch.setattr(chat_api, "get_agent_runtime_service", lambda: _LegacyRuntimeService())

    response = await chat_api.compact_conversation(
        42,
        current_user=_User(),
        db=_FakeDB(conversation),
    )

    assert response.context_snapshots
    assert response.context_snapshots[0].version == "conversation_context_snapshot.v1"
    assert response.context_snapshots[0].context_state is None
    assert response.context_snapshots[0].compacted_history is None


@pytest.mark.asyncio
async def test_get_conversation_sanitizes_legacy_empty_snapshot_payloads():
    conversation = Conversation(
        id=42,
        user_id=7,
        title="测试对话",
        llm_provider="aliyun",
        is_archived=0,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    conversation.messages = []
    conversation.metadata_ = {
        "turn_store": {
            "version": "conversation_turn_store.v1",
            "entries": [
                {
                    "turn_id": "turn:12",
                    "status": "completed",
                    "user_message_id": 12,
                    "assistant_message_id": 13,
                    "assistant_summary": "legacy summary",
                    "iteration_count": 1,
                    "tool_call_count": 0,
                    "tool_result_count": 0,
                }
            ],
        },
        "context_snapshots": [
            {
                "version": "conversation_context_snapshot.v1",
                "mode": "manual",
                "created_at": "2026-03-31T10:00:00",
                "summary_text": "legacy",
                "compacted_message_count": 2,
                "up_to_message_id": 12,
                "context_state": {},
                "compacted_history": {},
            }
        ]
    }

    response = await chat_api.get_conversation(
        42,
        current_user=_User(),
        db=_FakeDB(conversation),
    )

    assert response.context_snapshots
    assert response.context_snapshots[0].version == "conversation_context_snapshot.v1"
    assert response.context_snapshots[0].context_state is None
    assert response.context_snapshots[0].compacted_history is None
    assert response.turn_store is not None
    assert response.turn_store.entries[0].turn_id == "turn:12"


@pytest.mark.asyncio
async def test_get_conversation_normalizes_none_like_turn_store_values():
    conversation = Conversation(
        id=420,
        user_id=7,
        title="测试对话",
        llm_provider="aliyun",
        is_archived=0,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    conversation.messages = []
    conversation.metadata_ = {
        "turn_store": {
            "version": "conversation_turn_store.v1",
            "entries": [
                {
                    "turn_id": "turn:180",
                    "status": "completed",
                    "user_message_id": 180,
                    "assistant_message_id": 181,
                    "run_id": "None",
                    "assistant_summary": "None",
                    "error_message": "null",
                    "iteration_count": 0,
                    "tool_call_count": 0,
                    "tool_result_count": 0,
                }
            ],
        }
    }

    response = await chat_api.get_conversation(
        420,
        current_user=_User(),
        db=_FakeDB(conversation),
    )

    assert response.turn_store is not None
    assert response.turn_store.entries[0].run_id is None
    assert response.turn_store.entries[0].assistant_summary is None
    assert response.turn_store.entries[0].error_message is None


@pytest.mark.asyncio
async def test_get_conversation_sanitizes_legacy_message_metadata_payloads():
    conversation = Conversation(
        id=43,
        user_id=7,
        title="测试对话",
        llm_provider="aliyun",
        is_archived=0,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    conversation.messages = []
    conversation.metadata_ = {
        "item_stream": {
            "version": "conversation_item_stream.v1",
            "entries": [
                {
                    "item_id": "item-501",
                    "kind": "assistant_message",
                    "turn_id": "turn:501",
                    "role": "assistant",
                    "message_id": 501,
                    "content": "回答内容",
                    "metadata": {
                        "rag_metrics": {"knowledge_search_calls": 1},
                        "citation_index": {
                            "来源1": {
                                "label": "来源1",
                                "source_kind": "knowledge_base_search",
                                "knowledge_base": "Transformer",
                                "document": "Attention Is All You Need.pdf",
                            }
                        },
                        "reasoning_summary": {"summary": "先检索再回答"},
                        "context_debug": {"intent": "knowledge_query"},
                        "react_steps": [{"type": "thought", "content": "legacy"}],
                        "legacy_debug_key": "should_not_leak",
                    },
                    "created_at": datetime.utcnow().isoformat(),
                }
            ],
        }
    }

    response = await chat_api.get_conversation(
        43,
        current_user=_User(),
        db=_FakeDB(conversation),
    )

    assert len(response.messages) == 1
    assert response.messages[0].metadata == {
        "rag_metrics": {"knowledge_search_calls": 1},
        "citation_index": {
            "来源1": {
                "label": "来源1",
                "source_kind": "knowledge_base_search",
                "knowledge_base": "Transformer",
                "document": "Attention Is All You Need.pdf",
            }
        },
    }


@pytest.mark.asyncio
async def test_get_conversation_returns_structured_error_when_only_legacy_messages_exist():
    conversation = Conversation(
        id=44,
        user_id=7,
        title="旧对话",
        llm_provider="aliyun",
        is_archived=0,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    conversation.messages = [
        Message(
            id=601,
            conversation_id=44,
            role=MessageRole.ASSISTANT,
            content="旧回答",
            message_type=MessageType.TEXT,
            created_at=datetime.utcnow(),
        )
    ]
    conversation.metadata_ = {}

    with pytest.raises(chat_api.HTTPException) as exc_info:
        await chat_api.get_conversation(
            44,
            current_user=_User(),
            db=_FakeDB(conversation),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "conversation_item_stream_missing"
    assert exc_info.value.detail["conversation_id"] == 44


@pytest.mark.asyncio
async def test_get_messages_projects_from_item_stream(monkeypatch):
    conversation = Conversation(
        id=45,
        user_id=7,
        title="事件流对话",
        llm_provider="aliyun",
        is_archived=0,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    conversation.messages = []

    class _RuntimeWithItemStream:
        async def get_conversation_item_stream(self, conversation_id: int):
            assert conversation_id == 45
            return {
                "version": "conversation_item_stream.v1",
                "entries": [
                    {
                        "item_id": "i-user",
                        "kind": "user_message",
                        "turn_id": "turn:1",
                        "role": "user",
                        "message_id": 701,
                        "content": "问题",
                        "created_at": "2026-04-02T10:00:00",
                    },
                    {
                        "item_id": "i-tool",
                        "kind": "tool_result",
                        "turn_id": "turn:1",
                        "role": "tool",
                        "tool_name": "knowledge_search",
                        "tool_call_id": "call-1",
                        "summary": "不应出现在消息列表",
                        "created_at": "2026-04-02T10:00:01",
                    },
                    {
                        "item_id": "i-assistant",
                        "kind": "assistant_message",
                        "turn_id": "turn:1",
                        "role": "assistant",
                        "message_id": 702,
                        "content": "回答",
                        "thought": "推理摘要",
                        "metadata": {"rag_metrics": {"knowledge_search_calls": 1}},
                        "created_at": "2026-04-02T10:00:02",
                    },
                ],
            }

    monkeypatch.setattr(chat_api, "get_agent_runtime_service", lambda: _RuntimeWithItemStream())

    messages = await chat_api.get_messages(
        45,
        skip=0,
        limit=50,
        current_user=_User(),
        db=_FakeDB(conversation),
    )

    assert [msg.id for msg in messages] == [701, 702]
    assert messages[1].thought == "推理摘要"
    assert messages[1].metadata == {"rag_metrics": {"knowledge_search_calls": 1}}
