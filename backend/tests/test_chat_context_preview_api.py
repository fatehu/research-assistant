import os
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api import chat as chat_api
from app.models.conversation import Conversation
from app.schemas.chat import ChatContextPreviewRequest
from app.services.agent_runtime_service import AgentRuntimeService


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, conversation):
        self._conversation = conversation

    async def execute(self, _stmt):
        return _ScalarResult(self._conversation)


class _FakeAgent:
    async def prepare_context_preview(self, messages):
        assert messages[-1]["content"] == "继续解释"
        return SimpleNamespace(
            preview_mode="direct",
            system_prompt="system",
            llm_messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "解释注意力机制"},
                {"role": "assistant", "content": "注意力机制是一种动态聚焦机制"},
                {"role": "user", "content": "继续解释"},
            ],
            routing_decision={
                "intent": "general_chat",
                "carry_over_previous_goal": True,
                "needs_tools": False,
                "reason": "当前是在延续上一轮主题。",
            },
            context=SimpleNamespace(
                conversation_state={
                    "version": "conversation_context_state.v3",
                    "active_topic": "注意力机制",
                    "user_goal": "解释注意力机制",
                    "constraints": [],
                    "open_questions": [],
                    "resolved_facts": ["注意力机制是一种动态聚焦机制"],
                    "evidence_ledger": [],
                    "turn_count": 2,
                },
                compacted_history={
                    "version": "conversation_compacted_history.v2",
                    "history_anchors": "用户在讨论注意力机制。",
                    "history_summary": "已解释定义，正在继续展开。",
                    "compact_boundary_message_id": 20,
                    "replacement_history": [
                        {"role": "system", "content": "本轮继续展开注意力机制。"},
                    ],
                    "compacted_message_count": 0,
                },
                context_debug={
                    "version": "chat_context_debug.v1",
                    "intent": "general_chat",
                    "selected_tools": [],
                    "tool_choice": "auto",
                    "message_count_sent": 3,
                    "message_count_before_trim": 3,
                    "window_turns": 8,
                    "estimated_tokens": 128,
                    "budget": 4096,
                    "older_messages_count": 0,
                    "recent_messages_count": 3,
                    "context_truncated": False,
                    "recent_messages": [
                        {"role": "user", "content": "解释注意力机制"},
                        {"role": "assistant", "content": "注意力机制是一种动态聚焦机制"},
                        {"role": "user", "content": "继续解释"},
                    ],
                    "conversation_state": {
                        "version": "conversation_context_state.v3",
                        "active_topic": "注意力机制",
                        "user_goal": "解释注意力机制",
                        "constraints": [],
                        "open_questions": [],
                        "resolved_facts": ["注意力机制是一种动态聚焦机制"],
                        "evidence_ledger": [],
                        "turn_count": 2,
                    },
                }
            ),
        )

    async def prepare_direct_response(self, messages, *, force_no_tools=False):
        assert force_no_tools is True
        assert messages[-1]["content"] == "继续解释"
        return SimpleNamespace(
            system_prompt="direct-system",
            llm_messages=[
                {"role": "system", "content": "direct-system"},
                {"role": "user", "content": "继续解释"},
            ],
            routing_decision={
                "intent": "general_chat",
                "carry_over_previous_goal": False,
                "needs_tools": False,
                "reason": "显式禁用工具。",
            },
            context=SimpleNamespace(
                conversation_state={
                    "version": "conversation_context_state.v3",
                    "active_topic": "注意力机制",
                    "user_goal": "继续解释",
                    "constraints": [],
                    "open_questions": [],
                    "resolved_facts": [],
                    "evidence_ledger": [],
                    "turn_count": 2,
                },
                compacted_history={},
                context_debug={
                    "version": "chat_context_debug.v1",
                    "intent": "general_chat",
                    "selected_tools": [],
                    "tool_choice": "none",
                    "message_count_sent": 1,
                    "message_count_before_trim": 1,
                    "window_turns": 8,
                    "estimated_tokens": 32,
                    "budget": 4096,
                    "older_messages_count": 0,
                    "recent_messages_count": 1,
                    "context_truncated": False,
                    "recent_messages": [{"role": "user", "content": "继续解释"}],
                },
            ),
        )


class _FakeRuntimeService:
    @staticmethod
    def normalize_chat_preference_overrides(raw):
        payload = dict(raw or {}) if isinstance(raw, dict) else {}
        return {
            key: value
            for key, value in payload.items()
            if key in {"response_language", "response_verbosity", "web_search"}
        }

    @staticmethod
    def merge_chat_preferences(base, overrides):
        merged = dict(base or {})
        merged.update(dict(overrides or {}))
        if "version" not in merged:
            merged["version"] = "chat_preferences.v1"
        return merged

    @staticmethod
    def normalize_chat_rag_overrides(raw):
        payload = dict(raw or {}) if isinstance(raw, dict) else {}
        if not payload or not payload.get("enabled"):
            return {}
        normalized = {
            "version": "chat_rag_overrides.v1",
            "enabled": True,
            "scope_mode": payload.get("scope_mode") or "all",
            "knowledge_base_ids": list(payload.get("knowledge_base_ids") or []),
            "document_ids": list(payload.get("document_ids") or []),
        }
        for key in (
            "use_reranker",
            "use_hybrid",
            "use_query_rewrite",
            "query_rewrite_profile",
            "use_contextual_compression",
        ):
            if key in payload:
                normalized[key] = payload[key]
        return normalized

    @staticmethod
    def extract_chat_preference_candidates(*, draft_message: str, confirmed_preferences):
        assert draft_message == "继续解释"
        assert confirmed_preferences["response_language"] == "zh-CN"
        return [
            {
                "candidate_id": "cand-verbosity",
                "key": "response_verbosity",
                "suggested_value": "detailed",
                "reason": "草稿里出现了展开说明的诉求。",
                "source_excerpt": "继续解释",
                "source_kind": "draft",
            }
        ]

    async def get_conversation_revision(self, conversation_id: int | None):
        assert conversation_id == 54
        return "rev-54"

    async def get_conversation_context_state(self, conversation_id: int):
        assert conversation_id == 54
        return {
            "version": "conversation_context_state.v3",
            "active_topic": "注意力机制",
            "user_goal": "解释注意力机制",
            "constraints": [],
            "open_questions": [],
            "resolved_facts": ["注意力机制是一种动态聚焦机制"],
            "evidence_ledger": [],
            "turn_count": 2,
        }

    async def get_conversation_compacted_history(self, conversation_id: int):
        assert conversation_id == 54
        return {
            "version": "conversation_compacted_history.v2",
            "history_anchors": "用户在讨论注意力机制。",
            "history_summary": "已解释定义，正在继续展开。",
            "compact_boundary_message_id": 20,
            "replacement_history": [
                {"role": "system", "content": "本轮继续展开注意力机制。"},
            ],
            "compacted_message_count": 0,
        }

    async def get_conversation_history_log(self, conversation_id: int):
        assert conversation_id == 54
        return {"version": "conversation_history_log.v1", "events": []}

    async def get_conversation_tool_ledger(self, conversation_id: int):
        assert conversation_id == 54
        return {
            "version": "conversation_tool_ledger.v1",
            "entries": [
                {
                    "entry_id": "tool-result-1",
                    "kind": "tool_result",
                    "tool_name": "knowledge_search",
                    "turn_id": "turn:54",
                    "tool_call_id": "call_1",
                    "iteration": 1,
                    "status": "succeeded",
                    "summary": "已检索注意力机制定义。",
                    "success": True,
                }
            ],
        }

    async def get_conversation_turn_store(self, conversation_id: int):
        assert conversation_id == 54
        return {
            "version": "conversation_turn_store.v1",
            "entries": [
                {
                    "turn_id": "turn:54",
                    "status": "completed",
                    "user_message_id": 54,
                    "assistant_message_id": 55,
                    "assistant_summary": "注意力机制是一种动态聚焦机制。",
                    "iteration_count": 1,
                    "tool_call_count": 1,
                    "tool_result_count": 1,
                }
            ],
        }

    async def get_conversation_context_snapshots(self, conversation_id: int):
        assert conversation_id == 54
        return []

    async def get_conversation_item_stream(self, conversation_id: int):
        assert conversation_id == 54
        return {
            "version": "conversation_item_stream.v1",
            "entries": [
                {
                    "item_id": "item-1",
                    "kind": "user_message",
                    "turn_id": "turn:54",
                    "role": "user",
                    "content": "解释注意力机制",
                    "message_id": 54,
                },
                {
                    "item_id": "item-2",
                    "kind": "tool_result",
                    "turn_id": "turn:54",
                    "role": "tool",
                    "tool_name": "knowledge_search",
                    "tool_call_id": "call_1",
                    "iteration": 1,
                    "status": "succeeded",
                    "summary": "已检索注意力机制定义。",
                    "success": True,
                },
            ],
        }

    async def get_user_chat_preferences(self, *, user_id: int):
        assert user_id == 7
        return {
            "version": "chat_preferences.v1",
            "response_language": "zh-CN",
            "response_verbosity": "balanced",
            "web_search": "allow_when_needed",
        }

    def store_prepared_send_plan(self, **kwargs):
        assert kwargs["draft_message"] == "继续解释"
        assert kwargs["conversation_revision"] == "rev-54"
        return {
            "plan_id": "plan_preview_1",
            "preview_mode": "direct",
            "reusable": True,
            "draft_message": "继续解释",
            "draft_hash": "hash-preview-1",
            "conversation_revision": "rev-54",
            "message_count_sent": 3,
        }


class _User:
    id = 7
    preferred_llm_provider = "aliyun"


def test_sanitized_persisted_chat_metadata_drops_context_debug():
    payload = chat_api._sanitized_persisted_chat_metadata(
        {
            "rag_metrics": {"knowledge_search_calls": 1},
            "citation_index": {
                "来源1": {
                    "label": "来源1",
                    "source_kind": "knowledge_base_search",
                    "knowledge_base": "Transformer",
                    "document": "Attention Is All You Need.pdf",
                    "retrieval_scope": {"enabled": True, "scope_mode": "document", "document_ids": [34]},
                }
            },
            "context_debug": {"intent": "knowledge_query"},
            "reasoning_summary": {"summary": "采用知识检索回答。"},
            "turn_id": "turn:54",
        }
    )

    assert payload == {
        "rag_metrics": {"knowledge_search_calls": 1},
        "citation_index": {
            "来源1": {
                "label": "来源1",
                "source_kind": "knowledge_base_search",
                "knowledge_base": "Transformer",
                "document": "Attention Is All You Need.pdf",
                "retrieval_scope": {
                    "version": "chat_rag_overrides.v1",
                    "enabled": True,
                    "scope_mode": "document",
                    "knowledge_base_ids": [],
                    "document_ids": [34],
                },
            }
        },
    }


@pytest.mark.asyncio
async def test_preview_chat_context_returns_agent_prepared_preview(monkeypatch):
    conversation = Conversation(
        id=54,
        user_id=7,
        title="测试对话",
        llm_provider="aliyun",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    conversation.messages = []

    import app.services.react_agent as react_agent_module

    monkeypatch.setattr(react_agent_module, "create_chat_preview_planner", lambda *args, **kwargs: _FakeAgent())

    def _fake_get_tool_registry(*args, **kwargs):
        assert kwargs.get("initialize_mcp") is False
        return object()

    monkeypatch.setattr(chat_api, "get_tool_registry", _fake_get_tool_registry)
    monkeypatch.setattr(chat_api, "get_agent_runtime_service", lambda: _FakeRuntimeService())

    response = await chat_api.preview_chat_context(
        ChatContextPreviewRequest(message="继续解释", conversation_id=54),
        current_user=_User(),
        db=_FakeDB(conversation),
    )

    assert response.preview_mode == "direct"
    assert response.context_debug["intent"] == "general_chat"
    assert response.context_state is not None
    assert response.context_state.active_topic == "注意力机制"
    assert response.context_state.resolved_facts == ["注意力机制是一种动态聚焦机制"]
    assert response.compacted_history is not None
    assert response.compacted_history.history_anchors == "用户在讨论注意力机制。"
    assert response.compacted_history.compact_boundary_message_id == 20
    assert response.turn_store is not None
    assert response.turn_store.entries[0].turn_id == "turn:54"
    assert response.tool_ledger is not None
    assert response.tool_ledger.entries[0].turn_id == "turn:54"
    assert response.item_stream is not None
    assert response.item_stream.entries[0].turn_id == "turn:54"
    assert response.chat_preferences is not None
    assert response.chat_preferences["response_language"] == "zh-CN"
    assert response.effective_chat_preferences is not None
    assert response.effective_chat_preferences["response_language"] == "zh-CN"
    assert response.chat_preference_candidates is not None
    assert response.chat_preference_candidates[0]["candidate_id"] == "cand-verbosity"
    assert response.send_plan is not None
    assert response.send_plan["plan_id"] == "plan_preview_1"
    assert response.send_plan["conversation_revision"] == "rev-54"


@pytest.mark.asyncio
async def test_preview_chat_context_can_force_no_tools(monkeypatch):
    conversation = Conversation(
        id=54,
        user_id=7,
        title="测试对话",
        llm_provider="aliyun",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    conversation.messages = []

    import app.services.react_agent as react_agent_module

    monkeypatch.setattr(react_agent_module, "create_chat_preview_planner", lambda *args, **kwargs: _FakeAgent())

    def _fake_get_tool_registry(*args, **kwargs):
        assert kwargs.get("initialize_mcp") is False
        return object()

    monkeypatch.setattr(chat_api, "get_tool_registry", _fake_get_tool_registry)
    monkeypatch.setattr(chat_api, "get_agent_runtime_service", lambda: _FakeRuntimeService())

    response = await chat_api.preview_chat_context(
        ChatContextPreviewRequest(message="继续解释", conversation_id=54, use_tools=False),
        current_user=_User(),
        db=_FakeDB(conversation),
    )

    assert response.preview_mode == "direct"
    assert response.context_debug["tool_choice"] == "none"
    assert response.send_plan is not None
    assert response.send_plan["preview_mode"] == "direct"


@pytest.mark.asyncio
async def test_preview_chat_context_applies_chat_preference_overrides(monkeypatch):
    conversation = Conversation(
        id=54,
        user_id=7,
        title="测试对话",
        llm_provider="aliyun",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    conversation.messages = []

    import app.services.react_agent as react_agent_module

    captured_runtime_context = {}

    def _fake_create_chat_preview_planner(*args, **kwargs):
        captured_runtime_context.update(kwargs.get("runtime_context").chat_preferences_override or {})
        return _FakeAgent()

    monkeypatch.setattr(react_agent_module, "create_chat_preview_planner", _fake_create_chat_preview_planner)
    monkeypatch.setattr(chat_api, "get_tool_registry", lambda *args, **kwargs: object())
    monkeypatch.setattr(chat_api, "get_agent_runtime_service", lambda: _FakeRuntimeService())

    response = await chat_api.preview_chat_context(
        ChatContextPreviewRequest(
            message="继续解释",
            conversation_id=54,
            chat_preference_overrides={"response_verbosity": "detailed"},
        ),
        current_user=_User(),
        db=_FakeDB(conversation),
    )

    assert captured_runtime_context["response_verbosity"] == "detailed"
    assert response.effective_chat_preferences is not None
    assert response.effective_chat_preferences["response_verbosity"] == "detailed"


@pytest.mark.asyncio
async def test_preview_chat_context_applies_one_turn_rag_overrides(monkeypatch):
    conversation = Conversation(
        id=54,
        user_id=7,
        title="测试对话",
        llm_provider="aliyun",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    conversation.messages = []

    import app.services.react_agent as react_agent_module

    captured_runtime_context = {}

    def _fake_create_chat_preview_planner(*args, **kwargs):
        captured_runtime_context.update(kwargs.get("runtime_context").rag_overrides or {})
        return _FakeAgent()

    monkeypatch.setattr(react_agent_module, "create_chat_preview_planner", _fake_create_chat_preview_planner)
    monkeypatch.setattr(chat_api, "get_tool_registry", lambda *args, **kwargs: object())
    monkeypatch.setattr(chat_api, "get_agent_runtime_service", lambda: _FakeRuntimeService())

    response = await chat_api.preview_chat_context(
        ChatContextPreviewRequest(
            message="继续解释",
            conversation_id=54,
            rag_overrides={
                "enabled": True,
                "scope_mode": "document",
                "knowledge_base_ids": [12],
                "document_ids": [34],
                "use_reranker": True,
                "use_contextual_compression": False,
            },
        ),
        current_user=_User(),
        db=_FakeDB(conversation),
    )

    assert captured_runtime_context["scope_mode"] == "document"
    assert captured_runtime_context["document_ids"] == [34]
    assert response.effective_rag_overrides is not None
    assert response.effective_rag_overrides["knowledge_base_ids"] == [12]
    assert response.effective_rag_overrides["use_contextual_compression"] is False


def test_prepared_send_plan_requires_same_conversation_revision():
    service = AgentRuntimeService()
    stored = service.store_prepared_send_plan(
        user_id=7,
        conversation_id=54,
        llm_provider="aliyun",
        draft_message="继续解释",
        preview_mode="direct",
        conversation_revision="rev-54",
        system_prompt="system",
        llm_messages=[{"role": "user", "content": "继续解释"}],
        routing_decision={"intent": "general_chat"},
        rag_overrides={"enabled": True, "scope_mode": "document", "document_ids": [9]},
        prefetched_rag_messages=[{"role": "system", "content": "本轮 RAG 预取证据：\n片段A"}],
        prefetched_rag_metadata={"query": "继续解释", "result_count": 1},
    )

    reused = service.take_prepared_send_plan(
        plan_id=stored["plan_id"],
        user_id=7,
        conversation_id=54,
        draft_message="继续解释",
        llm_provider="aliyun",
        conversation_revision="rev-54",
    )
    assert reused is not None
    assert reused["rag_overrides"]["document_ids"] == [9]
    assert reused["prefetched_rag_messages"][0]["content"].startswith("本轮 RAG 预取证据：")
    assert reused["prefetched_rag_metadata"]["result_count"] == 1

    stored_again = service.store_prepared_send_plan(
        user_id=7,
        conversation_id=54,
        llm_provider="aliyun",
        draft_message="继续解释",
        preview_mode="direct",
        conversation_revision="rev-54",
        system_prompt="system",
        llm_messages=[{"role": "user", "content": "继续解释"}],
    )
    invalid = service.take_prepared_send_plan(
        plan_id=stored_again["plan_id"],
        user_id=7,
        conversation_id=54,
        draft_message="继续解释",
        llm_provider="aliyun",
        conversation_revision="rev-55",
    )
    assert invalid is None
