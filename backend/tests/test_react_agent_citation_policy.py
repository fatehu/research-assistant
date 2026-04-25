import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.agent_tools import ToolResult
from app.services.agent_profiles import resolve_agent_profile
from app.services.react_agent import (
    AgentContext,
    AgentRuntimeContext,
    AgentState,
    AgentStep,
    ExecutedToolCall,
    ParsedToolCall,
    ReActAgent,
    RoutingDecision,
)
from app.config import settings


def _tool_defs(*names: str):
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


class _DummyLLM:
    provider = "test"
    config = {"model": "test-model"}

    async def chat(self, *args, **kwargs):
        return {"content": "修正后的回答 [来源1]"}


class _HangingCitationLLM:
    provider = "test"
    config = {"model": "test-model"}

    async def chat(self, *args, **kwargs):
        import asyncio

        await asyncio.sleep(1)
        return {"content": "不会及时返回"}


class _DummyTools:
    def get_tools_description(self) -> str:
        return "- knowledge_search: 搜索知识库"

    def list_tools(self):
        return _tool_defs("knowledge_search")


class _SelectableTools:
    def __init__(self):
        self.calls = []

    def get_tools_description(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return "- web_search: 搜索互联网"

    def list_tools(self, **kwargs):
        return _tool_defs("web_search", "datetime", "calculator")


class _KnowledgeFollowupTools:
    def __init__(self):
        self.desc_calls = []

    def get_tools_description(self, **kwargs) -> str:
        self.desc_calls.append(kwargs)
        return "- knowledge_search: 搜索知识库"

    def list_tools(self, **kwargs):
        return _tool_defs("knowledge_search", "datetime", "calculator")


class _CodeFollowupTools:
    def __init__(self):
        self.desc_calls = []

    def get_tools_description(self, **kwargs) -> str:
        self.desc_calls.append(kwargs)
        return "- datetime: 时间\n- calculator: 计算器"

    def list_tools(self, **kwargs):
        return _tool_defs("datetime", "calculator")


class _SchemaCollectionTools:
    def __init__(self):
        self.list_calls = []

    def get_tools_description(self, **kwargs) -> str:
        return "- datetime: 时间\n- calculator: 计算器\n- knowledge_search: 知识库检索"

    def list_tools(self, **kwargs):
        self.list_calls.append(kwargs)
        if kwargs.get("include_tool_names"):
            names = set(kwargs["include_tool_names"])
        elif kwargs.get("intent"):
            names = {"datetime", "calculator"}
        else:
            names = {"datetime", "calculator", "knowledge_search"}

        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": name,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in sorted(names)
        ]


class _CodelabTools:
    def get_tools_description(self, **kwargs) -> str:
        return "- notebook_cell: 查看单元格\n- notebook_variables: 查看变量\n- notebook_execute: 执行代码\n- knowledge_search: 搜索知识库"

    def list_tools(self, **kwargs):
        return _tool_defs("notebook_cell", "notebook_variables", "notebook_execute", "knowledge_search")


class _IntentFilteredCodelabTools:
    route_profile = "codelab"
    notebook_id = "nb-filtered"
    kernel_manager = object()

    def __init__(self):
        self.list_calls = []

    def resolve_intent(self, user_text: str) -> str:
        _ = user_text
        return "code_task"

    def select_tool_names_for_intent(self, intent: str, user_text: str = ""):
        _ = (intent, user_text)
        return ["notebook_execute", "notebook_variables", "notebook_cell"]

    def get_tools_description(self, **kwargs) -> str:
        return "- notebook_execute: 执行代码\n- notebook_variables: 查看变量\n- notebook_cell: 查看单元格"

    def list_tools(self, **kwargs):
        self.list_calls.append(kwargs)
        names = kwargs.get("include_tool_names") or {"notebook_execute", "notebook_variables", "notebook_cell", "web_search"}
        return _tool_defs(*sorted(names))

    async def execute(self, tool_name: str, **kwargs):
        _ = kwargs
        return ToolResult(success=True, output=f"ok:{tool_name}", data={})


class _PaperWorkflowTools:
    def get_tools_description(self, **kwargs) -> str:
        return "- paper_research_status: 读取论文复现状态"

    def list_tools(self, **kwargs):
        return _tool_defs("paper_research_status", "activate_skill")

    async def execute(self, tool_name: str, **kwargs):
        _ = kwargs
        return ToolResult(success=True, output=f"ok:{tool_name}", data={})


class _PaperSelfWorkTools:
    def __init__(self):
        self.executed = []

    def get_tools_description(self, **kwargs) -> str:
        include = kwargs.get("include_tool_names")
        names = sorted(include) if include else [
            "activate_skill",
            "paper_research_status",
            "paper_research_prepare",
            "project_claude",
            "project_bash",
            "project_write_file",
            "paper_research_start_execution",
        ]
        return "\n".join(f"- {name}: {name}" for name in names)

    def list_tools(self, **kwargs):
        include = kwargs.get("include_tool_names")
        names = sorted(include) if include else [
            "activate_skill",
            "paper_research_status",
            "paper_research_prepare",
            "project_claude",
            "project_bash",
            "project_write_file",
            "paper_research_start_execution",
        ]
        return _tool_defs(*names)

    async def execute(self, tool_name: str, **kwargs):
        self.executed.append((tool_name, dict(kwargs)))
        if tool_name == "project_claude":
            return ToolResult(success=False, output="Project Claude 调用 runtime-worker 失败。", error="project_claude_worker_failed", data={})
        return ToolResult(success=True, output=f"ok:{tool_name}", data={})


class _RouterAwareCodelabTools:
    route_profile = "codelab"
    notebook_id = "nb-route"
    kernel_manager = object()
    user_authorized = False

    def __init__(self):
        self.desc_calls = []

    def get_tools_description(self, **kwargs) -> str:
        self.desc_calls.append(kwargs)
        return "- web_search: 搜索互联网\n- web_scrape: 抓取网页\n- notebook_cell: 查看单元格"

    def list_tools(self, **kwargs):
        return _tool_defs("web_search", "web_scrape", "notebook_cell", "code_analysis")


class _NoCompression:
    async def compress_chunks(self, *args, **kwargs):
        return []


class _CapturingKnowledgeTools:
    def __init__(self):
        self.calls = []

    def get_tools_description(self) -> str:
        return "- knowledge_search: 搜索知识库"

    def list_tools(self):
        return _tool_defs("knowledge_search")

    async def execute(self, tool_name: str, **kwargs):
        self.calls.append((tool_name, dict(kwargs)))
        return ToolResult(
            success=True,
            output="raw output",
            data={
                "results": [
                    {
                        "content": "命中的知识片段。",
                        "score": 0.9,
                        "knowledge_base": "测试知识库",
                        "document": "doc.md",
                        "chunk_index": 0,
                    }
                ]
            },
        )


class _CapturingMcpWebTools:
    def __init__(self):
        self.calls = []

    def get_tools_description(self) -> str:
        return "- mcp.firecrawl.firecrawl_search: 搜索网页"

    def list_tools(self):
        return _tool_defs("mcp.firecrawl.firecrawl_search")

    async def execute(self, tool_name: str, **kwargs):
        self.calls.append((tool_name, dict(kwargs)))
        return ToolResult(
            success=True,
            output='{"web":[{"url":"https://example.com/agentic-search","title":"Agentic Search","description":"An overview of agentic search."}]}',
            data={
                "source_kind": "public_web_search",
                "provider": "firecrawl",
                "results": [
                    {
                        "title": "Agentic Search",
                        "url": "https://example.com/agentic-search",
                        "snippet": "An overview of agentic search.",
                        "domain": "example.com",
                    }
                ],
                "provenance": {
                    "provider": "firecrawl",
                    "tool_kind": "web_search",
                },
            },
        )


class _FunctionCallingPreviewLLM:
    provider = "test"
    config = {"model": "test-model"}

    def supports_function_calling(self):
        return True


class _RecordingRuntimeService:
    def __init__(self):
        self.upsert_calls = []

    async def upsert_conversation_context_state(self, conversation_id: int, state):
        self.upsert_calls.append((conversation_id, dict(state or {})))


def test_system_prompt_contains_citation_policy():
    agent = ReActAgent(_DummyLLM(), _DummyTools(), max_iterations=1)
    prompt = agent._build_system_prompt()

    assert "知识检索引用规范" in prompt
    assert "[来源X]" in prompt
    assert "禁止编造不存在的来源编号" in prompt


def test_codelab_system_prompt_contains_dedicated_route_policy(monkeypatch):
    monkeypatch.setattr(settings, "tool_selection_enabled", True)
    agent = ReActAgent(
        _DummyLLM(),
        _CodelabTools(),
        max_iterations=1,
        runtime_context=AgentRuntimeContext(user_id=1, channel="codelab_agent", notebook_id="nb-1"),
    )
    agent._routing_decision = RoutingDecision(
        intent="code_task",
        intent_user_text="利用上传的数据集进行机器学习案例构建",
        carry_over_previous_goal=False,
        needs_tools=True,
        confidence=1.0,
        reason="当前轮在推进 notebook 任务。",
        source="llm_codelab",
        latest_user_text="利用上传的数据集进行机器学习案例构建",
    )

    prompt = agent._build_system_prompt(messages=[{"role": "user", "content": "利用上传的数据集进行机器学习案例构建"}])

    assert "CodeLab 场景规则" in prompt
    assert "默认先使用 `notebook_cell`、`notebook_variables`、`notebook_execute`" in prompt
    assert "不要调用 `knowledge_search`、`web_search`、`web_scrape` 或任何 `mcp.*` 工具" in prompt


def test_chat_profile_includes_chat_preferences_and_rag_sections():
    agent = ReActAgent(
        _DummyLLM(),
        _DummyTools(),
        max_iterations=1,
        runtime_context=AgentRuntimeContext(user_id=1, channel="chat"),
    )
    agent._active_chat_preferences = {
        "response_language": "zh-CN",
        "response_verbosity": "concise",
    }
    agent._active_rag_overrides = {
        "enabled": True,
        "scope_mode": "knowledge_base",
        "knowledge_base_ids": [7],
        "use_reranker": True,
    }

    prompt = agent._build_system_prompt(messages=[{"role": "user", "content": "帮我总结这个主题"}], function_calling=True)

    assert "用户已确认的聊天偏好" in prompt
    assert "本轮临时 RAG 注入" in prompt
    assert "检索范围: 仅限指定知识库 [7]" in prompt


def test_codelab_profile_excludes_chat_preference_and_rag_sections():
    agent = ReActAgent(
        _DummyLLM(),
        _CodelabTools(),
        max_iterations=1,
        runtime_context=AgentRuntimeContext(user_id=1, channel="codelab_agent", notebook_id="nb-1"),
    )
    agent._active_chat_preferences = {
        "response_language": "en-US",
        "response_verbosity": "detailed",
    }
    agent._active_rag_overrides = {
        "enabled": True,
        "scope_mode": "document",
        "document_ids": [11],
    }
    agent._active_channel_system_context = "Notebook snapshot: demo"

    prompt = agent._build_system_prompt(messages=[{"role": "user", "content": "继续调试当前 notebook"}], function_calling=True)

    assert "CodeLab / Notebook Runtime Context" in prompt
    assert "Notebook snapshot: demo" in prompt
    assert "用户已确认的聊天偏好" not in prompt
    assert "本轮临时 RAG 注入" not in prompt
    assert "知识检索引用规范" not in prompt


def test_profile_resolution_matches_route_boundaries():
    assert resolve_agent_profile("chat").key == "chat"
    assert resolve_agent_profile("codelab_agent").key == "codelab"
    assert resolve_agent_profile("notebook_agent").key == "codelab"
    assert resolve_agent_profile("literature").key == "literature"
    assert resolve_agent_profile("reader_compose").key == "default"


def test_system_prompt_uses_available_tools_without_intent_filtering(monkeypatch):
    monkeypatch.setattr(settings, "tool_selection_enabled", True)
    tools = _SelectableTools()
    agent = ReActAgent(_DummyLLM(), tools, max_iterations=1)

    prompt = agent._build_system_prompt(messages=[{"role": "user", "content": "今天最新新闻"}])

    assert "- web_search: 搜索互联网" in prompt
    assert tools.calls == [{}]
    assert agent._last_tool_selection["intent"] == "general_chat"
    assert agent._last_tool_selection["selected_tools"] == ["web_search", "datetime", "calculator"]
    assert agent._last_tool_selection["tool_choice"] == "auto"


def test_context_debug_keeps_assembled_messages_and_provider_messages_separate():
    agent = ReActAgent(_DummyLLM(), _DummyTools(), max_iterations=1)
    context = AgentContext(messages=[])

    agent._augment_context_debug_with_model_request(
        context=context,
        system_prompt="system prompt",
        llm_messages=[
            {"role": "user", "content": "解释注意力机制"},
            {"role": "assistant", "content": "", "thought": "这一轮已经检索到注意力机制的定义"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "knowledge_search", "arguments": "{\"query\":\"attention\"}"},
                    }
                ],
                "thought": "继续调用工具",
            },
        ],
        request_mode="direct",
    )

    assert context.context_debug["model_messages_assembled_raw"][1]["thought"] == "这一轮已经检索到注意力机制的定义"
    assert context.context_debug["model_messages_raw"] == [
        {"role": "user", "content": "解释注意力机制"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "knowledge_search", "arguments": "{\"query\":\"attention\"}"},
                }
            ],
        },
    ]


@pytest.mark.asyncio
async def test_prepare_direct_response_returns_none_for_non_heuristic_tool_like_prompt(monkeypatch):
    tools = _SelectableTools()
    agent = ReActAgent(_DummyLLM(), tools, max_iterations=1)

    async def _fake_prepare_runtime_context(context):
        return None

    monkeypatch.setattr(agent, "_prepare_runtime_context", _fake_prepare_runtime_context)

    prepared = await agent.prepare_direct_response([{"role": "user", "content": "联网查一下注意力机制"}])

    assert prepared is None


@pytest.mark.asyncio
async def test_prepare_direct_response_can_force_direct_without_tools(monkeypatch):
    tools = _SelectableTools()
    agent = ReActAgent(_DummyLLM(), tools, max_iterations=1)

    async def _fake_prepare_runtime_context(context):
        return None

    monkeypatch.setattr(agent, "_prepare_runtime_context", _fake_prepare_runtime_context)

    prepared = await agent.prepare_direct_response(
        [{"role": "user", "content": "联网查一下注意力机制"}],
        force_no_tools=True,
    )

    assert prepared is not None
    assert prepared.routing_decision is None
    assert "不要输出<think>" in prepared.system_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message_text",
    [
        "一句话解释注意力机制",
        "请直接用一句话解释注意力机制",
    ],
)
async def test_prepare_direct_response_no_longer_short_circuits_single_turn_direct_chat(
    monkeypatch,
    message_text,
):
    tools = _SelectableTools()
    agent = ReActAgent(_DummyLLM(), tools, max_iterations=1)

    async def _fake_prepare_runtime_context(context):
        return None

    monkeypatch.setattr(agent, "_prepare_runtime_context", _fake_prepare_runtime_context)

    prepared = await agent.prepare_direct_response([{"role": "user", "content": message_text}])

    assert prepared is None


@pytest.mark.asyncio
async def test_prepare_direct_response_returns_none_when_current_turn_rag_is_enabled(monkeypatch):
    tools = _SelectableTools()
    agent = ReActAgent(
        _DummyLLM(),
        tools,
        max_iterations=1,
        runtime_context=AgentRuntimeContext(
            user_id=1,
            channel="chat",
            rag_overrides={"enabled": True, "scope_mode": "all"},
        ),
    )

    async def _fake_prepare_runtime_context(context):
        context.active_rag_overrides = {"enabled": True, "scope_mode": "all"}
        agent._active_rag_overrides = dict(context.active_rag_overrides)

    monkeypatch.setattr(agent, "_prepare_runtime_context", _fake_prepare_runtime_context)

    prepared = await agent.prepare_direct_response([{"role": "user", "content": "一句话解释注意力机制"}])

    assert prepared is None
    assert agent._routing_decision is not None
    assert agent._routing_decision.source == "rag_override"
    assert agent._routing_decision.needs_tools is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message_text",
    [
        "再用两句话补充它解决了什么问题",
        "如果不使用它，会出现什么限制？",
    ],
)
async def test_prepare_direct_response_no_longer_short_circuits_followup_direct_chat(
    monkeypatch,
    message_text,
):
    tools = _SelectableTools()
    agent = ReActAgent(_DummyLLM(), tools, max_iterations=1)

    async def _fake_prepare_runtime_context(context):
        return None

    monkeypatch.setattr(agent, "_prepare_runtime_context", _fake_prepare_runtime_context)

    prepared = await agent.prepare_direct_response(
        [
            {"role": "user", "content": "请直接用一句话解释注意力机制"},
            {"role": "assistant", "content": "注意力机制让模型动态聚焦输入中的关键信息。"},
            {"role": "user", "content": message_text},
        ]
    )

    assert prepared is None


@pytest.mark.asyncio
async def test_prepare_context_preview_exposes_full_function_calling_request(monkeypatch):
    tools = _SelectableTools()
    agent = ReActAgent(_FunctionCallingPreviewLLM(), tools, max_iterations=1)

    async def _fake_prepare_runtime_context(context):
        return None

    monkeypatch.setattr(agent, "_prepare_runtime_context", _fake_prepare_runtime_context)

    prepared = await agent.prepare_context_preview([{"role": "user", "content": "联网查一下注意力机制最新文章"}])

    assert prepared.preview_mode == "agent"
    assert prepared.context.context_debug["model_request_mode"] == "function_calling"
    assert prepared.context.context_debug["model_system_prompt"] == prepared.system_prompt
    assert prepared.context.context_debug["model_messages_raw"]
    assert prepared.context.context_debug["model_tool_schemas_raw"]


@pytest.mark.asyncio
async def test_prepare_context_preview_prefetches_rag_evidence_and_injects_into_messages():
    tools = _CapturingKnowledgeTools()
    agent = ReActAgent(
        _FunctionCallingPreviewLLM(),
        tools,
        max_iterations=1,
        runtime_context=AgentRuntimeContext(
            channel="chat",
            rag_overrides={
                "enabled": True,
                "scope_mode": "knowledge_base",
                "knowledge_base_ids": [148],
                "use_reranker": True,
                "use_hybrid": True,
                "query_rewrite_profile": "light",
                "use_contextual_compression": True,
            },
        ),
    )
    agent.contextual_compression_service = _NoCompression()

    prepared = await agent.prepare_context_preview([{"role": "user", "content": "一句话解释注意力机制"}])

    assert prepared.preview_mode == "agent"
    assert prepared.routing_decision is not None
    assert prepared.routing_decision.source == "rag_override"
    assert prepared.routing_decision.needs_tools is True
    assert prepared.context.context_debug["model_request_mode"] == "function_calling"
    assert "本轮临时 RAG 注入" in prepared.system_prompt
    assert "系统会先按以上范围和策略预取一轮 `knowledge_search` 证据并注入当前上下文" in prepared.system_prompt
    assert tools.calls and tools.calls[0][0] == "knowledge_search"
    assert tools.calls[0][1]["query"] == "一句话解释注意力机制"
    assert tools.calls[0][1]["knowledge_base_ids"] == [148]
    assert tools.calls[0][1]["use_reranker"] is True
    assert tools.calls[0][1]["use_hybrid"] is True
    assert tools.calls[0][1]["query_rewrite_profile"] == "light"
    assert tools.calls[0][1]["use_contextual_compression"] is True
    assert prepared.context.context_debug["rag_prefetch_enabled"] is True
    assert prepared.context.context_debug["rag_prefetch_succeeded"] is True
    assert prepared.context.context_debug["rag_prefetch_reused_from_plan"] is False
    assert prepared.context.context_debug.get("rag_force_initial_knowledge_search") is None
    assert prepared.context.prefetched_rag_search_count == 1
    assert prepared.context.prefetched_rag_messages
    assert prepared.context.prefetched_rag_messages[0]["content"].startswith("本轮 RAG 预取证据：")
    assert any(
        str(item.get("content") or "").startswith("本轮 RAG 预取证据：")
        for item in prepared.llm_messages
        if isinstance(item, dict)
    )


def test_function_calling_system_prompt_does_not_repeat_tool_catalog():
    tools = _SelectableTools()
    agent = ReActAgent(_FunctionCallingPreviewLLM(), tools, max_iterations=1)

    prompt = agent._build_system_prompt(
        messages=[{"role": "user", "content": "联网查一下注意力机制最新文章"}],
        function_calling=True,
    )

    assert "tool/function schema" in prompt
    assert "- web_search: 搜索互联网" not in prompt
    assert tools.calls == []
    assert agent._last_tool_selection["prompt_desc_tokens"] == 0


def test_collect_llm_tool_schemas_uses_available_tools(monkeypatch):
    monkeypatch.setattr(settings, "tool_selection_enabled", True)
    tools = _SchemaCollectionTools()
    agent = ReActAgent(_DummyLLM(), tools, max_iterations=1)

    agent._build_system_prompt(messages=[{"role": "user", "content": "summarize my uploaded PDF"}])
    schemas = agent._collect_llm_tool_schemas("summarize my uploaded PDF")
    names = {item["function"]["name"] for item in schemas}

    assert agent._last_tool_selection["schema_scope"] == "available"
    assert names == {"datetime", "calculator", "knowledge_search"}
    assert tools.list_calls and tools.list_calls[-1] == {"include_tool_names": {"datetime", "calculator", "knowledge_search"}}


def test_codelab_system_prompt_uses_filtered_tool_selection(monkeypatch):
    monkeypatch.setattr(settings, "tool_selection_enabled", True)
    tools = _IntentFilteredCodelabTools()
    agent = ReActAgent(
        _DummyLLM(),
        tools,
        max_iterations=1,
        runtime_context=AgentRuntimeContext(user_id=1, channel="codelab_agent", notebook_id="nb-filtered"),
    )

    agent._build_system_prompt(messages=[{"role": "user", "content": "根据上传的 csv 在 notebook 里训练一个模型"}], function_calling=True)

    assert agent._last_tool_selection["intent"] == "code_task"
    assert agent._last_tool_selection["schema_scope"] == "selected"
    assert agent._last_tool_selection["selected_tools"] == [
        "notebook_cell",
        "notebook_execute",
        "notebook_variables",
    ]
    assert tools.list_calls and tools.list_calls[-1] == {
        "include_tool_names": {"notebook_execute", "notebook_variables", "notebook_cell"}
    }


@pytest.mark.asyncio
async def test_execute_single_tool_call_blocks_tools_outside_selected_allowlist():
    tools = _IntentFilteredCodelabTools()
    agent = ReActAgent(
        _DummyLLM(),
        tools,
        max_iterations=1,
        runtime_context=AgentRuntimeContext(user_id=1, channel="codelab_agent", notebook_id="nb-filtered"),
    )
    agent._last_tool_selection = {
        "selected_tools": ["notebook_execute", "notebook_variables", "notebook_cell"],
    }
    context = AgentContext(messages=[])

    executed = await agent._execute_single_tool_call(
        context,
        ParsedToolCall(
            call_id="call-web-search",
            name="web_search",
            arguments={"query": "latest news"},
            arguments_raw='{"query":"latest news"}',
        ),
        parallel_group="test",
    )

    assert executed.tool_name == "web_search"
    assert executed.success is False
    assert executed.error == "tool_not_allowed"
    assert "web_search" in executed.observation_output
    assert "notebook_execute" in executed.observation_output


@pytest.mark.asyncio
async def test_execute_single_tool_call_pins_paper_skill_after_successful_paper_tool():
    runtime_service = _RecordingRuntimeService()
    agent = ReActAgent(
        _DummyLLM(),
        _PaperWorkflowTools(),
        max_iterations=1,
        runtime_context=AgentRuntimeContext(user_id=1, channel="chat", conversation_id=321),
        runtime_service=runtime_service,
    )
    agent._last_tool_selection = {
        "selected_tools": ["paper_research_status", "activate_skill"],
    }
    context = AgentContext(messages=[], conversation_state={})

    executed = await agent._execute_single_tool_call(
        context,
        ParsedToolCall(
            call_id="call-paper-status",
            name="paper_research_status",
            arguments={"project_id": 3},
            arguments_raw='{"project_id":3}',
        ),
        parallel_group="test",
    )

    assert executed.success is True
    assert agent.runtime_context.active_skill_names == ["paper-reproduction"]
    assert context.conversation_state["active_skill_names"] == ["paper-reproduction"]
    assert agent._last_tool_selection["active_skill_names"] == ["paper-reproduction"]
    assert len(runtime_service.upsert_calls) == 2
    first_conversation_id, first_state = runtime_service.upsert_calls[0]
    assert first_conversation_id == 321
    assert first_state["workflow_binding"]["skill"] == "paper-reproduction"
    assert first_state["decision_state"]["repo_edit_allowed"] is False
    second_conversation_id, second_state = runtime_service.upsert_calls[1]
    assert second_conversation_id == 321
    assert second_state["active_skill_names"] == ["paper-reproduction"]
    assert second_state["active_skill_updated_at"] == context.conversation_state["active_skill_updated_at"]
    assert second_state["workflow_binding"]["skill"] == "paper-reproduction"
    assert second_state["decision_state"]["repo_edit_allowed"] is False


@pytest.mark.asyncio
async def test_execute_single_tool_call_does_not_duplicate_pinned_paper_skill():
    runtime_service = _RecordingRuntimeService()
    agent = ReActAgent(
        _DummyLLM(),
        _PaperWorkflowTools(),
        max_iterations=1,
        runtime_context=AgentRuntimeContext(
            user_id=1,
            channel="chat",
            conversation_id=321,
            active_skill_names=["paper-reproduction"],
        ),
        runtime_service=runtime_service,
    )
    agent._last_tool_selection = {
        "selected_tools": ["paper_research_status", "activate_skill"],
        "active_skill_names": ["paper-reproduction"],
    }
    context = AgentContext(messages=[], conversation_state={"active_skill_names": ["paper-reproduction"]})

    executed = await agent._execute_single_tool_call(
        context,
        ParsedToolCall(
            call_id="call-paper-status",
            name="paper_research_status",
            arguments={"project_id": 3},
            arguments_raw='{"project_id":3}',
        ),
        parallel_group="test",
    )

    assert executed.success is True
    assert agent.runtime_context.active_skill_names == ["paper-reproduction"]
    assert context.conversation_state["active_skill_names"] == ["paper-reproduction"]
    assert len(runtime_service.upsert_calls) == 1
    conversation_id, state = runtime_service.upsert_calls[0]
    assert conversation_id == 321
    assert state["active_skill_names"] == ["paper-reproduction"]
    assert state["workflow_binding"]["skill"] == "paper-reproduction"
    assert state["decision_state"]["repo_edit_allowed"] is False


def test_paper_reproduction_active_skill_hides_self_work_tools():
    tools = _PaperSelfWorkTools()
    agent = ReActAgent(
        _DummyLLM(),
        tools,
        max_iterations=1,
        runtime_context=AgentRuntimeContext(
            user_id=1,
            channel="chat",
            conversation_id=321,
            active_skill_names=["paper-reproduction"],
        ),
    )

    agent._build_system_prompt(messages=[{"role": "user", "content": "继续真实数据集复现"}])

    selected_tools = set(agent._last_tool_selection["selected_tools"])
    assert "project_claude" in selected_tools
    assert "paper_research_status" in selected_tools
    assert "project_bash" not in selected_tools
    assert "project_write_file" not in selected_tools
    assert "paper_research_start_execution" not in selected_tools


def test_paper_reproduction_active_skill_injects_session_system_prompt():
    tools = _PaperSelfWorkTools()
    agent = ReActAgent(
        _DummyLLM(),
        tools,
        max_iterations=1,
        runtime_context=AgentRuntimeContext(
            user_id=1,
            channel="chat",
            conversation_id=321,
            active_skill_names=["paper-reproduction"],
        ),
    )

    prompt = agent._build_system_prompt(messages=[{"role": "user", "content": "继续论文复现"}])

    assert "## Skill Session System Prompt" in prompt
    assert "你是策划者和决策者，不是实施 worker" in prompt
    assert "通过 `project_claude`" in prompt
    assert "不能作为 Claude Code 不可用或失败后的实施 fallback" in prompt
    assert "skill_system_prompt_tokens_estimate" not in prompt


@pytest.mark.asyncio
async def test_paper_reproduction_blocks_stale_project_bash_selection():
    tools = _PaperSelfWorkTools()
    agent = ReActAgent(
        _DummyLLM(),
        tools,
        max_iterations=1,
        runtime_context=AgentRuntimeContext(
            user_id=1,
            channel="chat",
            conversation_id=321,
            active_skill_names=["paper-reproduction"],
        ),
    )
    agent._last_tool_selection = {
        "selected_tools": ["project_bash", "project_claude", "activate_skill"],
        "active_skill_names": ["paper-reproduction"],
    }
    context = AgentContext(
        messages=[],
        conversation_state={
            "active_skill_names": ["paper-reproduction"],
            "workflow_binding": {"skill": "paper-reproduction", "project_id": 9, "paper_id": 113},
        },
    )

    executed = await agent._execute_single_tool_call(
        context,
        ParsedToolCall(
            call_id="call-project-bash",
            name="project_bash",
            arguments={"project_id": 9, "command": "python3 download_datasets.py --output-dir data"},
            arguments_raw='{"project_id":9}',
        ),
        parallel_group="test",
    )

    assert executed.success is False
    assert executed.error == "paper_reproduction_requires_claude_worker"
    assert "project_claude" in executed.observation_output
    assert "适用范围提示" in executed.observation_output
    assert tools.executed == []


@pytest.mark.asyncio
async def test_project_claude_failure_observation_mentions_no_bash_fallback():
    tools = _PaperSelfWorkTools()
    agent = ReActAgent(
        _DummyLLM(),
        tools,
        max_iterations=1,
        runtime_context=AgentRuntimeContext(
            user_id=1,
            channel="chat",
            conversation_id=321,
            active_skill_names=["paper-reproduction"],
        ),
    )
    agent._last_tool_selection = {
        "selected_tools": ["project_claude", "activate_skill"],
        "active_skill_names": ["paper-reproduction"],
    }
    context = AgentContext(
        messages=[],
        conversation_state={
            "active_skill_names": ["paper-reproduction"],
            "workflow_binding": {"skill": "paper-reproduction", "project_id": 9, "paper_id": 113},
        },
    )

    executed = await agent._execute_single_tool_call(
        context,
        ParsedToolCall(
            call_id="call-project-claude",
            name="project_claude",
            arguments={"project_id": 9, "prompt": "run real dataset reproduction"},
            arguments_raw='{"project_id":9}',
        ),
        parallel_group="test",
    )

    assert executed.success is False
    assert executed.error == "project_claude_worker_failed"
    assert "唯一项目执行 worker" in executed.observation_output
    assert "不能改用 `project_bash`" in executed.observation_output


def test_chat_prompt_uses_default_routing_without_router(monkeypatch):
    monkeypatch.setattr(settings, "tool_selection_enabled", True)
    tools = _KnowledgeFollowupTools()
    agent = ReActAgent(_DummyLLM(), tools, max_iterations=1)

    prompt = agent._build_system_prompt(
        messages=[
            {"role": "user", "content": "利用知识库，解释什么是 agentic search"},
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": "继续"},
        ]
    )

    assert "优先调用 `knowledge_search`" not in prompt
    assert agent._last_tool_selection["intent"] == "general_chat"
    assert agent._last_tool_selection["routing_source"] == "default_agent"


def test_apply_tool_call_overrides_backfills_paper_binding_for_paper_skill():
    agent = ReActAgent(_DummyLLM(), _PaperWorkflowTools(), max_iterations=1)

    effective_prepare = agent._apply_tool_call_overrides(
        "paper_research_prepare",
        {},
        workflow_binding={"skill": "paper-reproduction", "paper_id": 113, "project_id": 8},
    )
    effective_project_read = agent._apply_tool_call_overrides(
        "project_read_file",
        {"relative_path": "reference/paper/paper_interpretation.md"},
        workflow_binding={"skill": "paper-reproduction", "paper_id": 113, "project_id": 8},
    )
    explicit_args = agent._apply_tool_call_overrides(
        "paper_research_prepare",
        {"paper_id": 999, "project_id": 77},
        workflow_binding={"skill": "paper-reproduction", "paper_id": 113, "project_id": 8},
    )

    assert effective_prepare["paper_id"] == 113
    assert effective_prepare["project_id"] == 8
    assert effective_project_read["project_id"] == 8
    assert explicit_args["paper_id"] == 999
    assert explicit_args["project_id"] == 77


def test_system_prompt_includes_one_turn_rag_injection():
    agent = ReActAgent(
        _DummyLLM(),
        _DummyTools(),
        max_iterations=1,
        runtime_context=AgentRuntimeContext(
            user_id=1,
            rag_overrides={
                "enabled": True,
                "scope_mode": "document",
                "knowledge_base_ids": [12],
                "document_ids": [34],
                "use_reranker": True,
                "use_contextual_compression": False,
            },
        ),
    )
    agent._active_rag_overrides = dict(agent.runtime_context.rag_overrides)

    prompt = agent._build_system_prompt(messages=[{"role": "user", "content": "解释这个文档"}])

    assert "本轮临时 RAG 注入" in prompt
    assert "仅限指定文档 [34]" in prompt
    assert "contextual compression: 关闭" in prompt


def test_observation_message_for_knowledge_search_requires_citation():
    message = ReActAgent._build_observation_message(
        "knowledge_search",
        "[来源1] 这是检索结果",
    )

    assert "<observation>" in message
    assert "必须在关键结论后保留对应的 [来源X] 标注" in message
    assert "只能使用 observation 中出现过的来源编号" in message


def test_observation_message_for_other_tools_is_generic():
    message = ReActAgent._build_observation_message("calculator", "4")

    assert "<observation>" in message
    assert "请根据工具返回的信息继续。如果已有足够信息，请用<answer>标签给出最终回答。" in message


def test_observation_message_for_web_search_requires_web_citation():
    message = ReActAgent._build_observation_message(
        "web_search",
        "[网页1] 这是公网搜索结果",
    )

    assert "<observation>" in message
    assert "关键结论必须保留对应的 [网页X] 标注" in message
    assert "只能使用 observation 中出现过的网页编号" in message


@pytest.mark.asyncio
async def test_build_tool_result_ledger_entries_carries_structured_metadata():
    agent = ReActAgent(_DummyLLM(), _DummyTools(), max_iterations=1)
    context = AgentContext(messages=[], turn_id="turn:1", run_id="run-1", iteration=1)

    rows = await agent._build_tool_result_ledger_entries(
        context,
        [
            ExecutedToolCall(
                action_event={},
                observation_event={},
                tool_message={},
                tool_name="knowledge_search",
                observation_output="[来源1] 命中 Transformer / Attention Is All You Need.pdf",
                result_data={},
                tool_call_id="call-1",
                arguments={"query": "attention"},
                success=True,
                error=None,
                permission_required=False,
                execution_time_ms=10.0,
                output_tokens_estimate=42,
                truncated=False,
                metadata={
                    "source_kind": "knowledge_base_search",
                    "source_labels": ["来源1"],
                    "result_count": 1,
                    "retrieval_scope": {"knowledge_base_ids": [12], "document_ids": [34]},
                },
            )
        ],
    )

    assert rows[0]["metadata"]["source_kind"] == "knowledge_base_search"
    assert rows[0]["metadata"]["source_labels"] == ["来源1"]
    assert rows[0]["metadata"]["result_count"] == 1


def test_normalize_tool_result_metadata_carries_source_items():
    metadata = ReActAgent._normalize_tool_result_metadata(
        tool_name="knowledge_search",
        observation_output="[来源1] 命中 Transformer / Attention Is All You Need.pdf",
        result_data={
            "source_kind": "knowledge_base_search",
            "retrieval_scope": {"knowledge_base_ids": [12], "document_ids": [34]},
            "results": [
                {
                    "knowledge_base": "Transformer",
                    "document": "Attention Is All You Need.pdf",
                    "chunk_index": 4,
                    "reader_excerpt": "自注意力是核心机制。",
                }
            ],
        },
    )

    source_items = metadata.get("source_items") or []
    assert len(source_items) == 1
    assert source_items[0]["label"] == "来源1"
    assert source_items[0]["knowledge_base"] == "Transformer"
    assert source_items[0]["document"] == "Attention Is All You Need.pdf"
    assert source_items[0]["retrieval_scope"] == {"knowledge_base_ids": [12], "document_ids": [34]}


def test_extract_source_labels_and_validation():
    labels = ReActAgent._extract_source_labels("A[来源1] B[来源2] C")
    web_labels = ReActAgent._extract_web_source_labels("A[网页1] B[网页2] C")
    assert labels == {"1", "2"}
    assert web_labels == {"1", "2"}
    assert ReActAgent._citations_are_valid("结论 [来源1]", {"1", "2"}) is True
    assert ReActAgent._citations_are_valid("结论 [来源3]", {"1", "2"}) is False
    assert ReActAgent._citations_are_valid("结论无引用", {"1", "2"}) is False
    assert ReActAgent._citations_are_valid("结论 [网页1]", set(), {"1", "2"}) is True
    assert ReActAgent._citations_are_valid("结论 [来源1] [网页2]", {"1", "3"}, {"2"}) is True
    assert ReActAgent._citations_are_valid("结论 [网页3]", set(), {"1", "2"}) is False


def test_strip_unsupported_citation_tokens_removes_unbacked_labels():
    stripped = ReActAgent._strip_unsupported_citation_tokens(
        "这是直答内容 [网页1]，没有真实联网来源。",
        allowed_source_labels=set(),
        allowed_web_source_labels=set(),
    )

    assert "[网页1]" not in stripped
    assert "这是直答内容" in stripped


def test_direct_response_prompt_does_not_include_tool_citation_policy():
    agent = ReActAgent(_DummyLLM(), _DummyTools(), max_iterations=1)
    prompt = agent._build_direct_response_system_prompt()

    assert "当你基于 `knowledge_search` 返回内容作答时" not in prompt
    assert "不能编造新的来源编号" in prompt


def test_seed_allowed_citations_from_messages_recovers_prior_labels():
    context = AgentContext(
        messages=[
            {"role": "assistant", "content": "上一轮结论 [来源2] [网页3]"},
            {"role": "user", "content": "用户自己写的 [网页9] 不应计入"},
        ]
    )

    ReActAgent._seed_allowed_citations_from_messages(context)

    assert context.allowed_source_labels == {"2"}
    assert context.allowed_web_source_labels == {"3"}


@pytest.mark.asyncio
async def test_ensure_citation_compliance_repairs_answer():
    agent = ReActAgent(_DummyLLM(), _DummyTools(), max_iterations=1)
    context = AgentContext(messages=[])
    context.allowed_source_labels = {"1", "2"}

    fixed = await agent._ensure_citation_compliance("这是没有引用的回答", context)

    assert "[来源1]" in fixed
    assert context.citation_repair_attempts == 1
    assert context.citation_repair_successes == 1


@pytest.mark.asyncio
async def test_ensure_citation_compliance_times_out_to_note(monkeypatch):
    monkeypatch.setattr(settings, "agent_citation_repair_timeout_seconds", 0.01, raising=False)
    agent = ReActAgent(_HangingCitationLLM(), _DummyTools(), max_iterations=1)
    context = AgentContext(messages=[])
    context.allowed_source_labels = {"1", "2"}

    fixed = await agent._ensure_citation_compliance("这是没有引用的回答", context)

    assert "当前可用来源仅为 [来源1], [来源2]" in fixed
    assert context.citation_repair_attempts == 1
    assert context.citation_repair_successes == 0


@pytest.mark.asyncio
async def test_compress_observation_fallback_keeps_source_labels():
    agent = ReActAgent(_DummyLLM(), _DummyTools(), max_iterations=1)

    agent.contextual_compression_service = _NoCompression()

    result = ToolResult(
        success=True,
        output="raw output",
        data={
            "results": [
                {
                    "content": "Transformer 的核心是自注意力机制。",
                    "score": 0.88,
                    "knowledge_base": "深度学习基础",
                    "document": "chapter3.md",
                    "chunk_index": 3,
                }
            ]
        },
    )

    observation = await agent._compress_knowledge_observation("Transformer 核心是什么", result)
    assert "[来源1]" in observation


@pytest.mark.asyncio
async def test_compress_observation_can_disable_contextual_compression():
    agent = ReActAgent(_DummyLLM(), _DummyTools(), max_iterations=1)
    context = AgentContext(messages=[])
    context.active_rag_overrides = {
        "enabled": True,
        "use_contextual_compression": False,
    }

    result = ToolResult(
        success=True,
        output="raw output",
        data={
            "results": [
                {
                    "content": "Transformer 的核心是自注意力机制。",
                    "score": 0.88,
                    "knowledge_base": "深度学习基础",
                    "document": "chapter3.md",
                    "chunk_index": 3,
                }
            ]
        },
    )

    observation = await agent._compress_knowledge_observation("Transformer 核心是什么", result, context=context)
    assert "Knowledge contexts: 1" in observation
    assert "Compression score" not in observation
    assert "[来源1]" in observation


@pytest.mark.asyncio
async def test_compress_knowledge_observation_keeps_labels_unique_across_calls():
    agent = ReActAgent(_DummyLLM(), _DummyTools(), max_iterations=1)
    agent.contextual_compression_service = _NoCompression()
    context = AgentContext(messages=[])

    result = ToolResult(
        success=True,
        output="raw output",
        data={
            "results": [
                {
                    "content": "第一条知识库结果。",
                    "score": 0.91,
                    "knowledge_base": "深度学习基础",
                    "document": "chapter1.md",
                    "chunk_index": 1,
                },
                {
                    "content": "第二条知识库结果。",
                    "score": 0.82,
                    "knowledge_base": "深度学习基础",
                    "document": "chapter2.md",
                    "chunk_index": 2,
                },
            ]
        },
    )

    first = await agent._compress_knowledge_observation("第一次查询", result, context=context)
    second = await agent._compress_knowledge_observation("第二次查询", result, context=context)

    assert "[来源1]" in first and "[来源2]" in first
    assert "[来源3]" in second and "[来源4]" in second


@pytest.mark.asyncio
async def test_compress_web_observation_emits_distinct_web_labels():
    agent = ReActAgent(_DummyLLM(), _DummyTools(), max_iterations=1)
    context = AgentContext(messages=[])

    result = ToolResult(
        success=True,
        output="raw output",
        data={
            "results": [
                {
                    "title": "Attention Is All You Need overview",
                    "url": "https://example.com/attention",
                    "snippet": "A public explanation of transformer attention.",
                    "domain": "example.com",
                }
            ]
        },
    )

    first = await agent._compress_web_search_observation("attention", result, context=context)
    second = await agent._compress_web_search_observation("transformer", result, context=context)

    assert "[网页1]" in first
    assert "[网页2]" in second


@pytest.mark.asyncio
async def test_compress_web_observation_surfaces_direct_candidate_urls():
    agent = ReActAgent(_DummyLLM(), _DummyTools(), max_iterations=1)
    context = AgentContext(messages=[])

    result = ToolResult(
        success=True,
        output="raw output",
        data={
            "results": [
                {
                    "title": "automl/nanoTabPFN",
                    "url": "https://github.com/automl/nanoTabPFN",
                    "snippet": "repo page",
                    "domain": "github.com",
                    "candidate_download_urls": [
                        "http://ml.informatik.uni-freiburg.de/research-artifacts/nanoTabPFN/300k_150x5_2.h5"
                    ],
                }
            ]
        },
    )

    compressed = await agent._compress_web_search_observation('"300k_150x5_2.h5" nanoTabPFN', result, context=context)

    assert "Direct candidate URLs:" in compressed
    assert "300k_150x5_2.h5" in compressed


@pytest.mark.asyncio
async def test_compress_web_scrape_observation_preserves_full_page_content():
    agent = ReActAgent(_DummyLLM(), _DummyTools(), max_iterations=1)
    context = AgentContext(messages=[])
    markdown = "# Example Domain\nThis domain is for use in illustrative examples.\n\nMore detail here."

    result = ToolResult(
        success=True,
        output="raw output",
        data={
            "source_kind": "public_web_page",
            "url": "https://example.com",
            "source_domain": "example.com",
            "metadata": {"title": "Example Domain"},
            "markdown": markdown,
            "public_links": [
                {
                    "label": "Example Domain",
                    "href": "https://example.com",
                    "snippet": markdown,
                }
            ],
        },
    )

    compressed = await agent._compress_web_search_observation("example domain", result, context=context)

    assert "Content:\n# Example Domain" in compressed
    assert "This domain is for use in illustrative examples." in compressed


def test_build_rag_metrics_with_required_citation():
    context = AgentContext(messages=[])
    context.final_answer = "Transformer 的核心是自注意力机制 [来源1]"
    context.allowed_source_labels = {"1", "2"}
    context.knowledge_search_calls = 1
    context.compression_calls = 1
    context.compression_success_chunks = 1
    context.compression_fallback_chunks = 1

    metrics = ReActAgent._build_rag_metrics(context)
    assert metrics["knowledge_search_calls"] == 1
    assert metrics["source_labels_count"] == 1
    assert metrics["answer_citation_count"] == 1
    assert metrics["citation_required"] is True
    assert metrics["citation_valid"] is True
    assert metrics["source_labels"] == ["来源1"]
    assert metrics["available_source_labels"] == ["来源1", "来源2"]


def test_build_rag_metrics_with_web_and_knowledge_citations():
    context = AgentContext(messages=[])
    context.final_answer = "知识库说明注意力是核心机制 [来源1]，公网补充了发布时间背景 [网页1]"
    context.allowed_source_labels = {"1"}
    context.allowed_web_source_labels = {"1", "2"}
    context.knowledge_search_calls = 1
    context.web_search_calls = 2

    metrics = ReActAgent._build_rag_metrics(context)

    assert metrics["knowledge_search_calls"] == 1
    assert metrics["web_search_calls"] == 2
    assert metrics["source_labels_count"] == 2
    assert metrics["answer_citation_count"] == 2
    assert metrics["citation_required"] is True
    assert metrics["citation_valid"] is True
    assert metrics["source_labels"] == ["来源1", "网页1"]
    assert metrics["available_source_labels"] == ["来源1", "网页1", "网页2"]


class _ScriptedLLM:
    provider = "test"
    config = {"model": "test-model"}

    def __init__(self):
        self._call_count = 0

    async def chat(self, *args, **kwargs):
        self._call_count += 1
        if self._call_count == 1:
            return {
                "content": '<think>需要检索知识库</think>'
                '<action>{"tool":"knowledge_search","input":{"query":"Transformer 核心"}}'
                "</action>"
            }
        return {
            "content": "<think>根据检索结果整理答案</think>"
            "<answer>Transformer 核心是自注意力机制 [来源1]</answer>"
        }

    async def chat_stream(self, *args, **kwargs):
        yield ""


class _ScriptedTools:
    def get_tools_description(self) -> str:
        return "- knowledge_search: 搜索知识库"

    async def execute(self, tool_name: str, **kwargs):
        assert tool_name == "knowledge_search"
        return ToolResult(
            success=True,
            output="raw output",
            data={
                "results": [
                    {
                        "content": "Transformer 的核心是自注意力机制。",
                        "score": 0.92,
                        "knowledge_base": "深度学习基础",
                        "document": "chapter3.md",
                        "chunk_index": 3,
                    }
                ]
            },
        )


@pytest.mark.asyncio
async def test_run_done_event_contains_rag_metrics_baseline():
    agent = ReActAgent(_ScriptedLLM(), _ScriptedTools(), max_iterations=3)
    agent.contextual_compression_service = _NoCompression()

    events = []
    async for event in agent.run([{"role": "user", "content": "Transformer 核心是什么"}], stream=False):
        events.append(event)

    done_events = [event for event in events if event.get("type") == "done"]
    assert len(done_events) == 1

    payload = done_events[0]["data"]
    metrics = payload["rag_metrics"]
    assert metrics["knowledge_search_calls"] == 1
    assert metrics["source_labels_count"] == 1
    assert metrics["citation_required"] is True
    assert metrics["citation_valid"] is True
    assert metrics["answer_citation_count"] >= 1
    assert "来源1" in metrics["source_labels"]
    assert payload["citation_index"]["来源1"]["document"] == "chapter3.md"


@pytest.mark.asyncio
async def test_execute_single_tool_call_applies_rag_overrides_to_knowledge_search():
    tools = _CapturingKnowledgeTools()
    agent = ReActAgent(
        _DummyLLM(),
        tools,
        max_iterations=1,
        runtime_context=AgentRuntimeContext(
            user_id=1,
            rag_overrides={
                "enabled": True,
                "scope_mode": "document",
                "knowledge_base_ids": [12],
                "document_ids": [34],
                "use_reranker": True,
                "use_hybrid": False,
                "use_query_rewrite": False,
                "use_contextual_compression": False,
            },
        ),
    )
    agent.contextual_compression_service = _NoCompression()
    context = AgentContext(messages=[])
    context.active_rag_overrides = dict(agent.runtime_context.rag_overrides)
    agent._active_rag_overrides = dict(agent.runtime_context.rag_overrides)

    executed = await agent._execute_single_tool_call(
        context,
        agent._normalize_tool_calls([
            {
                "id": "call_1",
                "name": "knowledge_search",
                "arguments": "{\"query\":\"attention\"}",
            }
        ])[0],
        parallel_group="group_1",
    )

    assert tools.calls[0][0] == "knowledge_search"
    assert tools.calls[0][1]["knowledge_base_ids"] == [12]
    assert tools.calls[0][1]["document_ids"] == [34]
    assert tools.calls[0][1]["use_hybrid"] is False
    assert tools.calls[0][1]["use_contextual_compression"] is False
    assert executed.arguments["use_query_rewrite"] is False
    assert executed.action_event["data"]["input"]["use_reranker"] is True


@pytest.mark.asyncio
async def test_run_forces_initial_knowledge_search_when_one_turn_rag_is_enabled():
    tools = _CapturingKnowledgeTools()
    agent = ReActAgent(
        _DummyLLM(),
        tools,
        max_iterations=1,
        runtime_context=AgentRuntimeContext(
            rag_overrides={
                "enabled": True,
                "scope_mode": "all",
                "use_reranker": True,
            },
        ),
    )

    events = []
    async for event in agent.run([{"role": "user", "content": "请解释 agentic search"}], stream=False):
        events.append(event)

    assert tools.calls
    assert tools.calls[0][0] == "knowledge_search"
    assert tools.calls[0][1]["query"] == "请解释 agentic search"
    done_event = next(event for event in events if event.get("type") == "done")
    assert done_event["data"]["rag_metrics"]["prefetched_knowledge_search_count"] == 1


@pytest.mark.asyncio
async def test_execute_single_tool_call_treats_direct_mcp_web_search_as_citable_web_source():
    tools = _CapturingMcpWebTools()
    agent = ReActAgent(_DummyLLM(), tools, max_iterations=1)
    context = AgentContext(messages=[])

    executed = await agent._execute_single_tool_call(
        context,
        agent._normalize_tool_calls([
            {
                "id": "call_1",
                "name": "mcp.firecrawl.firecrawl_search",
                "arguments": "{\"query\":\"agentic search\"}",
            }
        ])[0],
        parallel_group="group_1",
    )

    assert tools.calls[0][0] == "mcp.firecrawl.firecrawl_search"
    assert "[网页1]" in executed.observation_output
    assert context.allowed_web_source_labels == {"1"}
    assert executed.metadata["source_items"][0]["label"] == "网页1"
    followup = agent._build_observation_message_multi([executed])
    assert "公网引用必须只使用 observation 已出现过的 [网页X]" in followup


def test_maybe_stop_after_background_execution_started_sets_final_answer():
    agent = ReActAgent(
        _DummyLLM(),
        _DummyTools(),
        max_iterations=1,
        runtime_context=AgentRuntimeContext(channel="chat"),
    )
    context = AgentContext(messages=[])

    thought = agent._maybe_stop_after_background_execution_started(
        context,
        [
            {
                "type": "observation",
                "data": {
                    "tool": "paper_research_start_execution",
                    "output": "长任务已转入后台执行。\nexecution_id=baseline_repro_fresh",
                    "data": {
                        "background_execution": {
                            "execution_id": "baseline_repro_fresh",
                            "status": "running",
                        },
                        "background_execution_started": True,
                        "background_execution_completed": False,
                    },
                },
            }
        ],
    )

    assert thought is not None
    assert context.state == AgentState.DONE
    assert "baseline_repro_fresh" in context.final_answer
    assert "后台执行" in context.final_answer
