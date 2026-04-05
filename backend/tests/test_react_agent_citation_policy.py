import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.agent_tools import ToolResult
from app.services.react_agent import AgentContext, AgentRuntimeContext, ReActAgent, RoutingDecision
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


class _FunctionCallingPreviewLLM:
    provider = "test"
    config = {"model": "test-model"}

    def supports_function_calling(self):
        return True


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
async def test_prepare_direct_response_short_circuits_obvious_single_turn_direct_chat(
    monkeypatch,
    message_text,
):
    tools = _SelectableTools()
    agent = ReActAgent(_DummyLLM(), tools, max_iterations=1)

    async def _fake_prepare_runtime_context(context):
        return None

    monkeypatch.setattr(agent, "_prepare_runtime_context", _fake_prepare_runtime_context)

    prepared = await agent.prepare_direct_response([{"role": "user", "content": message_text}])

    assert prepared is not None
    assert prepared.routing_decision is not None
    assert prepared.routing_decision.source == "heuristic_direct"
    assert prepared.routing_decision.needs_tools is False
    assert prepared.context.context_debug["model_request_mode"] == "direct"
    assert prepared.context.context_debug["model_system_prompt"] == prepared.system_prompt
    assert prepared.context.context_debug["model_messages_raw"] == prepared.llm_messages


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message_text",
    [
        "再用两句话补充它解决了什么问题",
        "如果不使用它，会出现什么限制？",
    ],
)
async def test_prepare_direct_response_short_circuits_obvious_followup_direct_chat(
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

    assert prepared is not None
    assert prepared.routing_decision is not None
    assert prepared.routing_decision.source == "heuristic_direct_followup"
    assert prepared.routing_decision.needs_tools is False
    assert prepared.routing_decision.carry_over_previous_goal is True


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
    assert metrics["source_labels_count"] == 2
    assert metrics["answer_citation_count"] == 1
    assert metrics["citation_required"] is True
    assert metrics["citation_valid"] is True
    assert metrics["source_labels"] == ["来源1", "来源2"]


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
    assert metrics["source_labels_count"] == 3
    assert metrics["answer_citation_count"] == 2
    assert metrics["citation_required"] is True
    assert metrics["citation_valid"] is True
    assert metrics["source_labels"] == ["来源1", "网页1", "网页2"]


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
    assert executed.arguments["use_query_rewrite"] is False
    assert executed.action_event["data"]["input"]["use_reranker"] is True
