import asyncio
import os
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core import database as core_database
from app.api import chat as chat_api
from app.models.conversation import Conversation, Message
from app.schemas.chat import ChatRequest, ChatSkillLaunch


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
        self._next_message_id = 1000

    async def execute(self, _stmt):
        stmt_text = str(_stmt)
        if "count(messages.id)" in stmt_text.lower():
            return _ScalarResult(len(list(getattr(self._conversation, "messages", []) or [])))
        return _ScalarResult(self._conversation)

    def add(self, obj):
        if isinstance(obj, Message):
            if getattr(obj, "id", None) is None:
                obj.id = self._next_message_id
                self._next_message_id += 1
            if getattr(obj, "created_at", None) is None:
                obj.created_at = datetime.utcnow()

    async def commit(self):
        return None

    async def refresh(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = self._next_message_id
            self._next_message_id += 1
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime.utcnow()
        return obj


class _FakeRuntimeService:
    def __init__(self):
        self.turn_entries = {}
        self.item_entries = []

    async def get_conversation_revision(self, conversation_id):
        return "rev-54"

    def take_prepared_send_plan(self, **kwargs):
        return None

    def normalize_chat_preference_overrides(self, raw):
        return dict(raw or {})

    def normalize_chat_rag_overrides(self, raw):
        payload = dict(raw or {})
        if not payload:
            return {}
        return payload

    async def cleanup_stale_conversation_turns(self, conversation_id, older_than_seconds):
        _ = conversation_id, older_than_seconds
        return 0

    async def append_conversation_item_entries(self, conversation_id, entries):
        for entry in entries:
            payload = dict(entry)
            if not payload.get("item_id"):
                payload["item_id"] = f"item:{len(self.item_entries) + 1}"
            self.item_entries.append(payload)

    async def upsert_conversation_turn_entry(self, conversation_id, payload):
        turn_id = str(payload.get("turn_id") or "").strip()
        existing = dict(self.turn_entries.get(turn_id) or {})
        merged = dict(existing)
        merged.update({key: value for key, value in dict(payload).items() if value is not None})
        self.turn_entries[turn_id] = merged

    async def get_conversation_turn_store(self, conversation_id):
        return {
            "version": "conversation_turn_store.v1",
            "updated_at": datetime.utcnow().isoformat(),
            "entries": list(self.turn_entries.values()),
        }

    async def get_conversation_context_state(self, conversation_id):
        return None

    async def get_conversation_tool_ledger(self, conversation_id):
        return None

    async def get_conversation_item_stream(self, conversation_id):
        return {
            "version": "conversation_item_stream.v1",
            "updated_at": datetime.utcnow().isoformat(),
            "entries": list(self.item_entries),
        }


class _NoopCompactionService:
    def enqueue_conversation(self, conversation_id):
        _ = conversation_id
        return None


class _FakeSaveSession:
    def __init__(self):
        self._next_message_id = 2000

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def add(self, obj):
        if isinstance(obj, Message):
            if getattr(obj, "id", None) is None:
                obj.id = self._next_message_id
                self._next_message_id += 1
            if getattr(obj, "created_at", None) is None:
                obj.created_at = datetime.utcnow()

    async def commit(self):
        return None

    async def refresh(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = self._next_message_id
            self._next_message_id += 1
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime.utcnow()
        return obj


class _FakeStreamingResponse:
    def __init__(self, iterator):
        self.body_iterator = iterator


async def _identity_skill_launch_request(request, *, user_id, db):
    _ = user_id, db
    return request


@pytest.fixture(autouse=True)
def _stub_compaction_service(monkeypatch):
    monkeypatch.setattr(chat_api, "get_conversation_context_compaction_service", lambda: _NoopCompactionService())
    monkeypatch.setattr(core_database, "async_session_factory", lambda: _FakeSaveSession())


@pytest.mark.asyncio
async def test_consume_streaming_response_events_collects_start_and_done():
    async def _iterator():
        yield chat_api._sse_event("start", {"turn_id": "turn:1", "conversation_id": 149}).encode("utf-8")
        yield chat_api._sse_event("done", {"answer": "ok", "run_id": "run-1"}).encode("utf-8")

    published = []

    async def _publish(event, data):
        published.append((event, data))

    start_payload, done_payload = await chat_api._consume_streaming_response_events(
        _FakeStreamingResponse(_iterator()),
        publish=_publish,
        idle_timeout_seconds=1.0,
    )

    assert start_payload["turn_id"] == "turn:1"
    assert done_payload["answer"] == "ok"
    assert [event for event, _ in published] == ["start", "done"]


@pytest.mark.asyncio
async def test_consume_streaming_response_events_raises_on_idle_timeout():
    async def _iterator():
        await asyncio.sleep(0.05)
        if False:
            yield b""

    async def _publish(event, data):
        return None

    with pytest.raises(RuntimeError, match="chat_stream_idle_timeout_after_0s|chat_stream_idle_timeout_after_1s"):
        await chat_api._consume_streaming_response_events(
            _FakeStreamingResponse(_iterator()),
            publish=_publish,
            idle_timeout_seconds=0.01,
        )


class _FakePlanner:
    def __init__(self, runtime_service):
        self.runtime_service = runtime_service
        self.seen_messages = []

    async def prepare_direct_response(self, messages, *, force_no_tools=False):
        self.seen_messages.append([dict(item) for item in messages])
        return SimpleNamespace(
            system_prompt="direct-system",
            llm_messages=[{"role": "user", "content": messages[-1]["content"]}],
            routing_decision={
                "intent": "general_chat",
                "carry_over_previous_goal": False,
                "needs_tools": False,
                "reason": "direct test",
            },
            context=SimpleNamespace(
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
                    "recent_messages": [{"role": "user", "content": messages[-1]["content"]}],
                }
            ),
        )


class _FakeLLMService:
    provider = "test"
    config = {"model": "test-model"}

    def __init__(self, provider=None):
        self.provider = provider or "test"

    async def chat_stream(self, *args, **kwargs):
        yield "partial"
        raise asyncio.CancelledError()

    async def chat(self, *args, **kwargs):
        return {
            "content": "direct answer",
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        }


class _FakeStreamingLLMService(_FakeLLMService):
    async def chat_stream(self, *args, **kwargs):
        yield "partial"
        yield " answer"


class _FakeSlowStreamingLLMService(_FakeLLMService):
    async def chat_stream(self, *args, **kwargs):
        yield "partial"
        await asyncio.sleep(30)


class _User:
    id = 7
    username = "tester"
    preferred_llm_provider = "aliyun"


class _FakeSkillService:
    def __init__(self):
        self.calls = []

    def render_launch_prompt(self, skill_name, payload):
        self.calls.append({"skill_name": skill_name, "payload": dict(payload or {})})
        return f"expanded::{payload.get('stage')}::{payload.get('paper_id')}"

    def get_skill(self, skill_name):
        if skill_name != "paper-reproduction":
            return None
        return SimpleNamespace(
            name="paper-reproduction",
            display_name="Paper Reproduction",
            stage_names=("planning", "execution", "tuning"),
            stage_policies=(
                "planning=manual",
                "execution=auto_continue",
                "tuning=ask_to_continue",
            ),
            default_continue_policy="ask_to_continue",
        )


@pytest.mark.parametrize(
    ("tool_name", "tool_payload"),
    [
        ("paper_research_inspect_runtime", {"project_id": 2}),
        ("paper_research_assess_repo_mainpath", {"project_id": 2}),
        ("paper_research_probe_repo", {"project_id": 2, "repo_url": "https://github.com/example/repo"}),
        ("paper_research_probe_url", {"project_id": 2, "url": "https://example.com/data.zip"}),
    ],
)
def test_build_tool_event_workflow_control_payload_promotes_probe_stage(monkeypatch, tool_name, tool_payload):
    skill_service = _FakeSkillService()
    monkeypatch.setattr(chat_api, "get_agent_skill_service", lambda: skill_service)

    workflow_control = chat_api._build_tool_event_workflow_control_payload(
        ChatRequest(
            message="继续论文规划阶段（paper_id=111）",
            conversation_id=58,
            stream=True,
            use_tools=True,
            skill_launch=ChatSkillLaunch(
                skill_name="paper-reproduction",
                stage="planning",
                paper_id=111,
                project_id=2,
            ),
        ),
        tool_name=tool_name,
        tool_payload=tool_payload,
        success=True,
        phase="action",
    )

    assert workflow_control is not None
    assert workflow_control["stage"] == "planning"
    assert workflow_control["stage_status"] == "running"
    assert workflow_control["continue_policy"] == "manual"
    assert workflow_control.get("next_stage") is None
    assert workflow_control.get("action") is None


@pytest.mark.asyncio
async def test_send_message_marks_turn_stopped_on_stream_cancellation(monkeypatch):
    conversation = Conversation(
        id=54,
        user_id=7,
        title="测试对话",
        llm_provider="aliyun",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    conversation.messages = []
    runtime_service = _FakeRuntimeService()

    import app.services.react_agent as react_agent_module

    monkeypatch.setattr(chat_api, "get_agent_runtime_service", lambda: runtime_service)
    monkeypatch.setattr(chat_api, "get_tool_registry", lambda *args, **kwargs: object())
    monkeypatch.setattr(react_agent_module, "create_chat_preview_planner", lambda *args, **kwargs: _FakePlanner(runtime_service))
    monkeypatch.setattr(chat_api, "LLMService", _FakeLLMService)

    response = await chat_api.send_message(
        ChatRequest(
            message="这是一个会被取消的流式请求。",
            conversation_id=54,
            stream=True,
            use_tools=False,
        ),
        current_user=_User(),
        db=_FakeDB(conversation),
    )

    chunks = []
    with pytest.raises(asyncio.CancelledError):
        async for chunk in response.body_iterator:
            chunks.append(chunk)

    joined = "".join(
        chunk.decode("utf-8") if isinstance(chunk, (bytes, bytearray)) else str(chunk)
        for chunk in chunks
    )
    assert '"event": "start"' in joined
    assert '"event": "content"' in joined

    turn_store = await runtime_service.get_conversation_turn_store(54)
    entry = turn_store["entries"][0]
    assert entry["status"] == "stopped"
    assert entry["error_message"] == "stream_cancelled"
    assert entry.get("assistant_message_id") is None
    assert entry["assistant_summary"] == "partial"


@pytest.mark.asyncio
async def test_send_message_marks_turn_stopped_on_consumer_cancellation(monkeypatch):
    conversation = Conversation(
        id=56,
        user_id=7,
        title="测试对话",
        llm_provider="aliyun",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    conversation.messages = []
    runtime_service = _FakeRuntimeService()

    import app.services.react_agent as react_agent_module

    monkeypatch.setattr(chat_api, "get_agent_runtime_service", lambda: runtime_service)
    monkeypatch.setattr(chat_api, "get_tool_registry", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        react_agent_module,
        "create_chat_preview_planner",
        lambda *args, **kwargs: _FakePlanner(runtime_service),
    )
    monkeypatch.setattr(chat_api, "LLMService", _FakeSlowStreamingLLMService)

    response = await chat_api.send_message(
        ChatRequest(
            message="这是一个由外部取消的流式请求。",
            conversation_id=56,
            stream=True,
            use_tools=False,
        ),
        current_user=_User(),
        db=_FakeDB(conversation),
    )

    content_seen = asyncio.Event()
    chunks = []

    async def _consume():
        async for chunk in response.body_iterator:
            text = chunk.decode("utf-8") if isinstance(chunk, (bytes, bytearray)) else str(chunk)
            chunks.append(text)
            if '"event": "content"' in text:
                content_seen.set()

    task = asyncio.create_task(_consume())
    await asyncio.wait_for(content_seen.wait(), timeout=1.0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    joined = "".join(chunks)
    assert '"event": "start"' in joined
    assert '"event": "content"' in joined

    turn_store = await runtime_service.get_conversation_turn_store(56)
    entry = turn_store["entries"][0]
    assert entry["status"] == "stopped"
    assert entry["error_message"] == "stream_cancelled"
    assert entry.get("assistant_message_id") is None
    assert entry["assistant_summary"] == "partial"


@pytest.mark.asyncio
async def test_send_message_prefers_light_planner_for_direct_response_when_tools_enabled(monkeypatch):
    conversation = Conversation(
        id=55,
        user_id=7,
        title="测试对话",
        llm_provider="aliyun",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    conversation.messages = []
    runtime_service = _FakeRuntimeService()

    import app.services.react_agent as react_agent_module

    registry_calls = []

    def _fake_get_tool_registry(*args, **kwargs):
        registry_calls.append(dict(kwargs))
        return object()

    def _fail_create_react_agent(*args, **kwargs):
        raise AssertionError("direct response should not initialize full react agent")

    monkeypatch.setattr(chat_api, "get_agent_runtime_service", lambda: runtime_service)
    monkeypatch.setattr(chat_api, "get_tool_registry", _fake_get_tool_registry)
    monkeypatch.setattr(react_agent_module, "create_chat_preview_planner", lambda *args, **kwargs: _FakePlanner(runtime_service))
    monkeypatch.setattr(react_agent_module, "create_react_agent", _fail_create_react_agent)
    monkeypatch.setattr(chat_api, "LLMService", _FakeLLMService)

    response = await chat_api.send_message(
        ChatRequest(
            message="请直接回答这个问题，不需要工具。",
            conversation_id=55,
            stream=False,
            use_tools=True,
        ),
        current_user=_User(),
        db=_FakeDB(conversation),
    )

    assert response["message"].content == "direct answer"
    assert len(registry_calls) == 1
    assert registry_calls[0]["initialize_mcp"] is False


@pytest.mark.asyncio
async def test_send_message_stream_emits_phase_events_before_direct_answer(monkeypatch):
    conversation = Conversation(
        id=56,
        user_id=7,
        title="测试对话",
        llm_provider="aliyun",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    conversation.messages = []
    runtime_service = _FakeRuntimeService()

    import app.services.react_agent as react_agent_module

    monkeypatch.setattr(chat_api, "get_agent_runtime_service", lambda: runtime_service)
    monkeypatch.setattr(chat_api, "get_tool_registry", lambda *args, **kwargs: object())
    monkeypatch.setattr(react_agent_module, "create_chat_preview_planner", lambda *args, **kwargs: _FakePlanner(runtime_service))
    monkeypatch.setattr(chat_api, "LLMService", _FakeStreamingLLMService)

    response = await chat_api.send_message(
        ChatRequest(
            message="一句话解释注意力机制。",
            conversation_id=56,
            stream=True,
            use_tools=True,
        ),
        current_user=_User(),
        db=_FakeDB(conversation),
    )

    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)

    joined = "".join(
        chunk.decode("utf-8") if isinstance(chunk, (bytes, bytearray)) else str(chunk)
        for chunk in chunks
    )
    assert '"event": "start"' in joined
    assert '"event": "phase"' in joined
    assert '"key": "loading_context"' in joined
    assert '"key": "routing"' in joined
    assert '"key": "waiting_model"' in joined
    assert '"event": "content"' in joined
    assert '"event": "done"' in joined


def test_chat_request_accepts_skill_launch_without_message():
    request = ChatRequest(
        skill_launch=ChatSkillLaunch(
            skill_name="paper-reproduction",
            stage="planning",
            paper_id=111,
        ),
        stream=True,
    )

    assert request.message is None
    assert request.skill_launch is not None
    assert request.skill_launch.skill_name == "paper-reproduction"


@pytest.mark.asyncio
async def test_send_message_uses_skill_launch_renderer_but_persists_visible_message(monkeypatch):
    conversation = Conversation(
        id=57,
        user_id=7,
        title="测试对话",
        llm_provider="aliyun",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    conversation.messages = []
    runtime_service = _FakeRuntimeService()
    planner = _FakePlanner(runtime_service)
    skill_service = _FakeSkillService()

    import app.services.react_agent as react_agent_module

    monkeypatch.setattr(chat_api, "get_agent_runtime_service", lambda: runtime_service)
    monkeypatch.setattr(chat_api, "get_agent_skill_service", lambda: skill_service)
    monkeypatch.setattr(chat_api, "get_tool_registry", lambda *args, **kwargs: object())
    monkeypatch.setattr(chat_api, "_normalize_skill_launch_request", _identity_skill_launch_request)
    monkeypatch.setattr(react_agent_module, "create_chat_preview_planner", lambda *args, **kwargs: planner)
    monkeypatch.setattr(chat_api, "LLMService", _FakeLLMService)

    response = await chat_api.send_message(
        ChatRequest(
            message="继续论文规划阶段（paper_id=111）",
            conversation_id=57,
            stream=False,
            use_tools=True,
            skill_launch=ChatSkillLaunch(
                skill_name="paper-reproduction",
                stage="planning",
                paper_id=111,
                project_id=2,
                goal="run baseline",
            ),
        ),
        current_user=_User(),
        db=_FakeDB(conversation),
    )

    assert response["message"].content == "direct answer"
    assert skill_service.calls == [
        {
            "skill_name": "paper-reproduction",
            "payload": {
                "skill_name": "paper-reproduction",
                "stage": "planning",
                "paper_id": 111,
                "project_id": 2,
                "goal": "run baseline",
            },
        }
    ]
    assert planner.seen_messages[-1][-1]["content"] == "expanded::planning::111"
    assert response["workflow_control"]["next_stage"] == "execution"
    turn_store = await runtime_service.get_conversation_turn_store(57)
    assert turn_store["entries"][0]["user_content"] == "继续论文规划阶段（paper_id=111）"


@pytest.mark.asyncio
async def test_send_message_stream_emits_workflow_control_for_skill_launch(monkeypatch):
    conversation = Conversation(
        id=58,
        user_id=7,
        title="测试对话",
        llm_provider="aliyun",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    conversation.messages = []
    runtime_service = _FakeRuntimeService()
    planner = _FakePlanner(runtime_service)
    skill_service = _FakeSkillService()

    import app.services.react_agent as react_agent_module

    monkeypatch.setattr(chat_api, "get_agent_runtime_service", lambda: runtime_service)
    monkeypatch.setattr(chat_api, "get_agent_skill_service", lambda: skill_service)
    monkeypatch.setattr(chat_api, "get_tool_registry", lambda *args, **kwargs: object())
    monkeypatch.setattr(chat_api, "_normalize_skill_launch_request", _identity_skill_launch_request)
    monkeypatch.setattr(react_agent_module, "create_chat_preview_planner", lambda *args, **kwargs: planner)
    monkeypatch.setattr(chat_api, "LLMService", _FakeStreamingLLMService)

    response = await chat_api.send_message(
        ChatRequest(
            message="继续论文规划阶段（paper_id=111）",
            conversation_id=58,
            stream=True,
            use_tools=True,
            skill_launch=ChatSkillLaunch(
                skill_name="paper-reproduction",
                stage="planning",
                paper_id=111,
                project_id=2,
            ),
        ),
        current_user=_User(),
        db=_FakeDB(conversation),
    )

    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode("utf-8") if isinstance(chunk, (bytes, bytearray)) else str(chunk))

    joined = "".join(chunks)
    assert '"event": "done"' in joined
    assert '"workflow_control"' in joined
    assert '"next_stage": "execution"' in joined
    assert '继续 执行阶段' in joined


@pytest.mark.asyncio
async def test_send_message_stream_emits_running_workflow_control_on_probe_tool(monkeypatch):
    class _ToolPlanner:
        def __init__(self, runtime_service):
            self.runtime_service = runtime_service

        async def prepare_direct_response(self, messages, *, force_no_tools=False):
            _ = messages, force_no_tools
            return None

    class _ToolAgent:
        def __init__(self, runtime_service):
            self.runtime_service = runtime_service

        async def run(self, agent_messages, stream=True, prepared_plan=None):
            _ = agent_messages, stream, prepared_plan
            yield {"type": "start", "data": {"provider": "test", "model": "tool-model"}}
            yield {
                "type": "action",
                "data": {
                    "tool": "paper_research_inspect_runtime",
                    "input": {"project_id": 2},
                },
            }
            yield {
                "type": "observation",
                "data": {
                    "tool": "paper_research_inspect_runtime",
                    "success": True,
                    "output": "runtime ok",
                    "data": {"project_id": 2},
                },
            }
            yield {
                "type": "done",
                "data": {
                    "answer": "runtime inspected",
                    "run_id": "run-1",
                },
            }

    conversation = Conversation(
        id=59,
        user_id=7,
        title="测试对话",
        llm_provider="aliyun",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    conversation.messages = []
    runtime_service = _FakeRuntimeService()
    skill_service = _FakeSkillService()

    import app.services.react_agent as react_agent_module

    monkeypatch.setattr(chat_api, "get_agent_runtime_service", lambda: runtime_service)
    monkeypatch.setattr(chat_api, "get_agent_skill_service", lambda: skill_service)
    monkeypatch.setattr(chat_api, "get_tool_registry", lambda *args, **kwargs: object())
    monkeypatch.setattr(chat_api, "_normalize_skill_launch_request", _identity_skill_launch_request)
    monkeypatch.setattr(react_agent_module, "create_chat_preview_planner", lambda *args, **kwargs: _ToolPlanner(runtime_service))
    monkeypatch.setattr(react_agent_module, "create_react_agent", lambda *args, **kwargs: _ToolAgent(runtime_service))
    monkeypatch.setattr(chat_api, "LLMService", _FakeStreamingLLMService)

    response = await chat_api.send_message(
        ChatRequest(
            message="继续论文规划阶段（paper_id=111）",
            conversation_id=59,
            stream=True,
            use_tools=True,
            skill_launch=ChatSkillLaunch(
                skill_name="paper-reproduction",
                stage="planning",
                paper_id=111,
                project_id=2,
            ),
        ),
        current_user=_User(),
        db=_FakeDB(conversation),
    )

    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode("utf-8") if isinstance(chunk, (bytes, bytearray)) else str(chunk))

    joined = "".join(chunks)
    assert '"event": "action"' in joined
    assert '"stage": "planning"' in joined
    assert '"stage_status": "running"' in joined
    assert '"event": "done"' in joined


@pytest.mark.asyncio
async def test_normalize_skill_launch_request_resolves_project_and_builds_reference(monkeypatch, tmp_path):
    paper = SimpleNamespace(
        id=111,
        title="Attention Is All You Need",
        year=2017,
        venue="NeurIPS",
        arxiv_id="1706.03762",
    )
    captured = {}

    async def _fake_get_owned_paper(db, *, user_id, paper_id):
        _ = db
        captured["paper_lookup"] = {"user_id": user_id, "paper_id": paper_id}
        return paper

    async def _fake_resolve_project(db, *, user_id, paper, project_id, goal):
        _ = db, paper
        captured["project_resolution"] = {"user_id": user_id, "project_id": project_id, "goal": goal}
        return {"id": 9, "primary_paper_id": 111}

    class _FakeBuilder:
        def __init__(self, db):
            captured["builder_db"] = db

        @classmethod
        def reference_bundle_ready(cls, project_dir):
            captured["project_dir"] = str(project_dir)
            return False

        async def build(self, *, paper, project_id, user_id, refresh=False):
            captured["builder_call"] = {
                "paper_id": int(paper.id),
                "project_id": int(project_id),
                "user_id": int(user_id),
                "refresh": bool(refresh),
            }
            return {"reference_ready": True}

    monkeypatch.setattr(chat_api, "_get_owned_paper_for_skill_launch", _fake_get_owned_paper)
    monkeypatch.setattr(chat_api, "_resolve_project_payload_for_skill_launch", _fake_resolve_project)
    monkeypatch.setattr(chat_api, "ProjectReferenceBuilderService", _FakeBuilder)
    monkeypatch.setattr(chat_api, "get_project_root_dir", lambda project_id: tmp_path / str(int(project_id)))

    normalized = await chat_api._normalize_skill_launch_request(
        ChatRequest(
            skill_launch=ChatSkillLaunch(
                skill_name="paper-reproduction",
                stage="planning",
                paper_id=111,
                goal="run baseline",
            ),
            stream=True,
        ),
        user_id=7,
        db=object(),
    )

    assert normalized.skill_launch is not None
    assert normalized.skill_launch.paper_id == 111
    assert normalized.skill_launch.project_id == 9
    assert captured["paper_lookup"] == {"user_id": 7, "paper_id": 111}
    assert captured["project_resolution"] == {"user_id": 7, "project_id": None, "goal": "run baseline"}
    assert captured["builder_call"] == {
        "paper_id": 111,
        "project_id": 9,
        "user_id": 7,
        "refresh": False,
    }


@pytest.mark.asyncio
async def test_send_message_normalizes_paper_skill_launch_before_rendering(monkeypatch):
    conversation = Conversation(
        id=61,
        user_id=7,
        title="测试对话",
        llm_provider="aliyun",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    conversation.messages = []
    runtime_service = _FakeRuntimeService()
    planner = _FakePlanner(runtime_service)
    skill_service = _FakeSkillService()

    async def _normalize_request(request, *, user_id, db):
        _ = user_id, db
        return request.model_copy(
            update={
                "skill_launch": request.skill_launch.model_copy(
                    update={"project_id": 9}
                )
            }
        )

    import app.services.react_agent as react_agent_module

    monkeypatch.setattr(chat_api, "get_agent_runtime_service", lambda: runtime_service)
    monkeypatch.setattr(chat_api, "get_agent_skill_service", lambda: skill_service)
    monkeypatch.setattr(chat_api, "get_tool_registry", lambda *args, **kwargs: object())
    monkeypatch.setattr(chat_api, "_normalize_skill_launch_request", _normalize_request)
    monkeypatch.setattr(react_agent_module, "create_chat_preview_planner", lambda *args, **kwargs: planner)
    monkeypatch.setattr(chat_api, "LLMService", _FakeLLMService)

    response = await chat_api.send_message(
        ChatRequest(
            message="继续论文规划阶段（paper_id=111）",
            conversation_id=61,
            stream=False,
            use_tools=True,
            skill_launch=ChatSkillLaunch(
                skill_name="paper-reproduction",
                stage="planning",
                paper_id=111,
            ),
        ),
        current_user=_User(),
        db=_FakeDB(conversation),
    )

    assert response["message"].content == "direct answer"
    assert skill_service.calls[-1]["payload"]["project_id"] == 9
    assert planner.seen_messages[-1][-1]["content"] == "expanded::planning::111"


@pytest.mark.asyncio
async def test_create_chat_background_run_binds_conversation_after_stream_start(monkeypatch):
    class _FakeBackgroundRuntimeService:
        def __init__(self):
            self.bind_calls = []
            self.complete_calls = []
            self.turn_entries = []
            self.call_sequence = []

        async def create_run(self, **kwargs):
            self.create_kwargs = dict(kwargs)
            return "bg-run-1"

        async def bind_run_to_conversation(self, run_id, *, conversation_id):
            self.bind_calls.append({"run_id": run_id, "conversation_id": conversation_id})
            self.call_sequence.append(("bind", conversation_id))

        async def complete_run(self, run_id, **kwargs):
            self.complete_calls.append({"run_id": run_id, **dict(kwargs)})
            self.call_sequence.append(("complete", kwargs.get("metadata", {}).get("conversation_id")))

        async def append_chat_run_event(self, *args, **kwargs):
            return None

        async def upsert_conversation_turn_entry(self, conversation_id, payload):
            self.turn_entries.append({"conversation_id": conversation_id, "payload": dict(payload)})

    class _ImmediateBackgroundManager:
        async def start(self, *, run_id, user_id, execute_fn, persist_event_fn=None):
            _ = user_id, persist_event_fn

            async def _publish(_event, _data):
                return None

            await execute_fn(_publish)
            return {"run_id": run_id, "status": "completed"}

    async def _fake_send_message(request, current_user, db):
        _ = request, current_user, db

        async def _iterator():
            yield chat_api._sse_event("start", {"conversation_id": 88, "turn_id": "turn:88"}).encode("utf-8")
            yield chat_api._sse_event("done", {"answer": "ok", "run_id": "inner-run-1"}).encode("utf-8")

        return chat_api.StreamingResponse(_iterator(), media_type="text/event-stream")

    runtime_service = _FakeBackgroundRuntimeService()
    monkeypatch.setattr(chat_api, "get_agent_runtime_service", lambda: runtime_service)
    monkeypatch.setattr(chat_api, "get_chat_background_run_manager", lambda: _ImmediateBackgroundManager())
    monkeypatch.setattr(chat_api, "send_message", _fake_send_message)

    response = await chat_api.create_chat_background_run(
        ChatRequest(message="后台继续跑", stream=True),
        current_user=_User(),
    )

    assert response["run_id"] == "bg-run-1"
    assert runtime_service.bind_calls == [{"run_id": "bg-run-1", "conversation_id": 88}]
    assert runtime_service.complete_calls[0]["run_id"] == "bg-run-1"
    assert runtime_service.complete_calls[0]["metadata"]["conversation_id"] == 88
    assert runtime_service.call_sequence == [("bind", 88), ("complete", 88)]


@pytest.mark.asyncio
async def test_send_message_stream_persists_thought_items_into_item_stream(monkeypatch):
    class _ToolPlanner:
        def __init__(self, runtime_service):
            self.runtime_service = runtime_service

        async def prepare_direct_response(self, messages, *, force_no_tools=False):
            _ = messages, force_no_tools
            return None

    class _ThoughtAgent:
        def __init__(self, runtime_service):
            self.runtime_service = runtime_service
            self.runtime_context = SimpleNamespace(run_id="run-thought-1")

        async def run(self, agent_messages, stream=True, prepared_plan=None):
            _ = agent_messages, stream, prepared_plan
            yield {"type": "start", "data": {"provider": "test", "model": "tool-model"}}
            yield {"type": "thinking_start", "data": {"iteration": 1}}
            yield {"type": "thought", "data": "让我先确认一下脚本入口。"}
            yield {
                "type": "done",
                "data": {
                    "answer": "已经确认入口脚本。",
                    "run_id": "run-thought-1",
                },
            }

    conversation = Conversation(
        id=60,
        user_id=7,
        title="测试 thought 持久化",
        llm_provider="aliyun",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    conversation.messages = []
    runtime_service = _FakeRuntimeService()

    import app.services.react_agent as react_agent_module

    monkeypatch.setattr(chat_api, "get_agent_runtime_service", lambda: runtime_service)
    monkeypatch.setattr(chat_api, "get_tool_registry", lambda *args, **kwargs: object())
    monkeypatch.setattr(react_agent_module, "create_chat_preview_planner", lambda *args, **kwargs: _ToolPlanner(runtime_service))
    monkeypatch.setattr(react_agent_module, "create_react_agent", lambda *args, **kwargs: _ThoughtAgent(runtime_service))
    monkeypatch.setattr(chat_api, "LLMService", _FakeStreamingLLMService)

    response = await chat_api.send_message(
        ChatRequest(
            message="帮我看下现在做到哪一步了",
            conversation_id=60,
            stream=True,
            use_tools=True,
        ),
        current_user=_User(),
        db=_FakeDB(conversation),
    )

    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode("utf-8") if isinstance(chunk, (bytes, bytearray)) else str(chunk))

    thought_entries = [entry for entry in runtime_service.item_entries if entry.get("kind") == "thought"]
    assert thought_entries
    assert thought_entries[0]["content"] == "让我先确认一下脚本入口。"
    assert thought_entries[0]["iteration"] == 1

    joined = "".join(chunks)
    assert '"event": "done"' in joined
    assert '"kind": "thought"' in joined
