import asyncio
import os
import sys
import types
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services import agent_tools, notebook_tools


def _tool_result(success: bool, output: str, error: str | None = None):
    return agent_tools.ToolResult(success=success, output=output, error=error)


class _EchoInput(BaseModel):
    text: str = Field(min_length=1)


class _RetryingTool(agent_tools.ToolBase):
    name = "retrying_tool"
    description = "retry tool for tests"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    input_model = _EchoInput
    timeout_seconds = 0.01
    retry_count = 1

    def __init__(self):
        self.calls = 0

    def _resolve_timeout_seconds(self) -> float:
        return 0.01

    async def _execute(self, text: str):
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(0.05)
        return agent_tools.ToolResult(success=True, output=text)


class _LongOutputTool(agent_tools.ToolBase):
    name = "long_output"
    description = "long output tool for tests"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    output_max_tokens = 32

    async def _execute(self, text: str):
        return agent_tools.ToolResult(success=True, output=f"HEAD-{text}-" + ("x" * 2000) + "-TAIL")


@pytest.mark.asyncio
async def test_tool_base_validation_error_is_structured():
    tool = _RetryingTool()
    result = await tool.execute(text="")

    assert result.success is False
    assert result.error == "validation_error"
    assert isinstance(result.data, dict)
    assert "validation_errors" in result.data
    assert result.execution_time_ms >= 0
    assert result.output_tokens_estimate > 0
    assert result.truncated is False


@pytest.mark.asyncio
async def test_tool_base_timeout_then_retry_success():
    tool = _RetryingTool()
    result = await tool.execute(text="ok")

    assert result.success is True
    assert tool.calls == 2
    assert result.data["retry_attempt"] == 2
    assert result.execution_time_ms > 0
    assert result.output_tokens_estimate > 0


@pytest.mark.asyncio
async def test_tool_base_truncates_long_output_and_keeps_head_tail():
    tool = _LongOutputTool()
    result = await tool.execute(text="probe")

    assert result.success is True
    assert result.truncated is True
    assert "...[TRUNCATED]..." in result.output
    assert "HEAD-probe-" in result.output
    assert "-TAIL" in result.output


@pytest.mark.asyncio
async def test_web_search_prefers_tavily_when_available(monkeypatch):
    tool = agent_tools.WebSearchTool()
    tool.serper_api_key = "x"
    tool.tavily_api_key = "y"
    calls = []

    async def _serper(*args, **kwargs):
        calls.append("serper")
        return _tool_result(True, "serper ok")

    async def _tavily(*args, **kwargs):
        calls.append("tavily")
        return _tool_result(True, "tavily ok")

    async def _ddgs(*args, **kwargs):
        calls.append("ddgs")
        return _tool_result(True, "ddgs ok")

    monkeypatch.setattr(tool, "_serper_search", _serper)
    monkeypatch.setattr(tool, "_tavily_search", _tavily)
    monkeypatch.setattr(tool, "_ddgs_search", _ddgs)

    result = await tool._execute("q", max_results=3)
    assert result.success is True
    assert result.output == "tavily ok"
    assert calls == ["tavily"]


@pytest.mark.asyncio
async def test_web_search_fallback_chain(monkeypatch):
    tool = agent_tools.WebSearchTool()
    tool.serper_api_key = "x"
    tool.tavily_api_key = "y"
    calls = []

    async def _tavily(*args, **kwargs):
        calls.append("tavily")
        return _tool_result(False, "tavily failed", error="tavily_down")

    async def _serper(*args, **kwargs):
        calls.append("serper")
        return _tool_result(False, "serper failed", error="serper_down")

    async def _ddgs(*args, **kwargs):
        calls.append("ddgs")
        return _tool_result(True, "ddgs ok")

    monkeypatch.setattr(tool, "_serper_search", _serper)
    monkeypatch.setattr(tool, "_tavily_search", _tavily)
    monkeypatch.setattr(tool, "_ddgs_search", _ddgs)

    result = await tool._execute("q", max_results=3)
    assert result.success is True
    assert result.output == "ddgs ok"
    assert calls == ["tavily", "serper", "ddgs"]


@pytest.mark.asyncio
async def test_web_search_fallback_chain_when_provider_raises_request_error(monkeypatch):
    tool = agent_tools.WebSearchTool()
    tool.serper_api_key = "x"
    tool.tavily_api_key = "y"
    calls = []

    async def _tavily(*args, **kwargs):
        calls.append("tavily")
        raise httpx.ConnectError(
            "dns failed",
            request=httpx.Request("POST", "https://api.tavily.com/search"),
        )

    async def _serper(*args, **kwargs):
        calls.append("serper")
        return _tool_result(False, "serper failed", error="serper_down")

    async def _ddgs(*args, **kwargs):
        calls.append("ddgs")
        return _tool_result(True, "ddgs ok")

    monkeypatch.setattr(tool, "_serper_search", _serper)
    monkeypatch.setattr(tool, "_tavily_search", _tavily)
    monkeypatch.setattr(tool, "_ddgs_search", _ddgs)

    result = await tool._execute("q", max_results=3)
    assert result.success is True
    assert result.output == "ddgs ok"
    assert calls == ["tavily", "serper", "ddgs"]


@pytest.mark.asyncio
async def test_web_search_all_providers_failed(monkeypatch):
    tool = agent_tools.WebSearchTool()
    tool.serper_api_key = "x"
    tool.tavily_api_key = "y"

    async def _failed(*args, **kwargs):
        return _tool_result(False, "failed", error="down")

    monkeypatch.setattr(tool, "_serper_search", _failed)
    monkeypatch.setattr(tool, "_tavily_search", _failed)
    monkeypatch.setattr(tool, "_ddgs_search", _failed)

    result = await tool._execute("q", max_results=3)
    assert result.success is False
    assert result.error == "web_search_all_failed"


class _FakeSearchResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeSearchClient:
    def __init__(self, payload: dict):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        return _FakeSearchResponse(self._payload)


@pytest.mark.asyncio
async def test_web_search_serper_payload_is_guided_reading_ready(monkeypatch):
    tool = agent_tools.WebSearchTool()
    tool.serper_api_key = "x"
    payload = {
        "answerBox": {
            "answer": "USMLE includes Step 1, Step 2 CK, and Step 3.",
            "link": "https://www.usmle.org/",
        },
        "organic": [
            {
                "title": "USMLE Overview",
                "link": "https://www.usmle.org/",
                "snippet": "Official overview of the exam sequence and purpose.",
            }
        ],
    }
    monkeypatch.setattr(agent_tools.httpx, "AsyncClient", lambda **kwargs: _FakeSearchClient(payload))

    result = await tool._serper_search("usmle overview", max_results=3)

    assert result.success is True
    assert result.data["source_kind"] == "public_web_search"
    assert result.data["provider_route"] == "local.web_search.serper"
    assert result.data["reader_summary"]
    assert result.data["provenance"]["tool_kind"] == "web_search"
    assert result.data["structured_content"]["results"][0]["rank"] == 1
    assert result.data["structured_content"]["results"][0]["domain"] == "usmle.org"
    assert result.data["structured_content"]["results"][0]["is_authoritative_source"] is True
    assert result.data["public_links"][0]["href"] == "https://www.usmle.org/"
    assert result.data["structured_content"]["domains"][0]["domain"] == "usmle.org"


@pytest.mark.asyncio
async def test_web_search_extracts_direct_candidate_urls_from_snippet(monkeypatch):
    tool = agent_tools.WebSearchTool()
    tool.serper_api_key = "x"
    payload = {
        "organic": [
            {
                "title": "automl/nanoTabPFN",
                "link": "https://github.com/automl/nanoTabPFN",
                "snippet": (
                    "curl http://ml.informatik.uni-freiburg.de/research-artifacts/"
                    "nanoTabPFN/300k_150x5_2.h5 --output 300k_150x5_2.h5"
                ),
            }
        ],
    }
    monkeypatch.setattr(agent_tools.httpx, "AsyncClient", lambda **kwargs: _FakeSearchClient(payload))

    result = await tool._serper_search('"300k_150x5_2.h5" nanoTabPFN', max_results=3)

    assert result.success is True
    row = result.data["structured_content"]["results"][0]
    assert row["embedded_urls"] == [
        "http://ml.informatik.uni-freiburg.de/research-artifacts/nanoTabPFN/300k_150x5_2.h5"
    ]
    assert row["candidate_download_urls"] == [
        "http://ml.informatik.uni-freiburg.de/research-artifacts/nanoTabPFN/300k_150x5_2.h5"
    ]
    assert result.data["candidate_download_urls"][0]["matched_filename"] == "300k_150x5_2.h5"


@pytest.mark.asyncio
async def test_web_search_preserves_full_snippet_in_payload(monkeypatch):
    tool = agent_tools.WebSearchTool()
    tool.serper_api_key = "x"
    long_snippet = " ".join(f"segment-{index}" for index in range(80))
    payload = {
        "organic": [
            {
                "title": "Long Snippet Result",
                "link": "https://example.com/long",
                "snippet": long_snippet,
            }
        ],
    }
    monkeypatch.setattr(agent_tools.httpx, "AsyncClient", lambda **kwargs: _FakeSearchClient(payload))

    result = await tool._serper_search("long snippet", max_results=3)

    assert result.success is True
    row = result.data["structured_content"]["results"][0]
    assert row["reader_excerpt"] == long_snippet
    assert result.data["public_links"][0]["snippet"] == long_snippet
    assert result.data["reader_summary"] == f"Long Snippet Result: {long_snippet}"


class _FakeEvalError:
    def __init__(self, message: str):
        self._message = message

    def get_error(self):
        return ("error", self._message)


class _FakeInterpreter:
    def __init__(self, usersyms=None, minimal=True):
        self.usersyms = usersyms or {}
        self.error = []

    def __call__(self, expr: str):
        try:
            return eval(expr, {"__builtins__": {}}, dict(self.usersyms))
        except Exception as exc:  # pragma: no cover - defensive in fake
            self.error = [_FakeEvalError(str(exc))]
            return None


@pytest.mark.asyncio
async def test_calculator_rejects_unsafe_expression():
    tool = agent_tools.CalculatorTool()
    result = await tool.execute(expression="__import__('os').system('echo x')")

    assert result.success is False
    assert result.error == "unsafe_expression"


@pytest.mark.asyncio
async def test_calculator_safe_expression_with_fake_asteval(monkeypatch):
    monkeypatch.setitem(sys.modules, "asteval", types.SimpleNamespace(Interpreter=_FakeInterpreter))
    tool = agent_tools.CalculatorTool()
    result = await tool.execute(expression="2+3*4")

    assert result.success is True
    assert result.data["result"] == 14


def _fake_tool(name: str):
    return SimpleNamespace(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
    )


def _patch_registry_defaults(monkeypatch):
    monkeypatch.setattr(agent_tools, "KnowledgeSearchTool", lambda db, user_id, db_session_factory=None: _fake_tool("knowledge_search"))
    monkeypatch.setattr(agent_tools, "WebSearchTool", lambda: _fake_tool("web_search"))
    monkeypatch.setattr(agent_tools, "CalculatorTool", lambda: _fake_tool("calculator"))
    monkeypatch.setattr(agent_tools, "DateTimeTool", lambda: _fake_tool("datetime"))
    monkeypatch.setattr(agent_tools, "TextAnalysisTool", lambda: _fake_tool("text_analysis"))
    monkeypatch.setattr(agent_tools, "UnitConverterTool", lambda: _fake_tool("unit_converter"))
    monkeypatch.setattr(agent_tools, "LiteratureSearchTool", lambda: _fake_tool("literature_search"))


def test_codelab_tool_selection_filters_tools_and_keeps_fallback(monkeypatch):
    _patch_registry_defaults(monkeypatch)
    monkeypatch.setattr(agent_tools.settings, "tool_selection_enabled", True)
    monkeypatch.setattr(agent_tools.settings, "tool_selection_fallback_tools", "datetime,calculator")

    registry = agent_tools.ToolRegistry(db=object(), user_id=1, route_profile="codelab")
    selected = set(registry.select_tool_names_for_intent("knowledge_query"))

    assert {"knowledge_search", "datetime", "calculator"}.issubset(selected)
    assert "web_search" not in selected

    desc = registry.get_tools_description(intent="web_query", user_text="today latest news")
    assert "web_search" in desc
    assert "datetime" in desc
    assert "calculator" in desc
    assert "knowledge_search" not in desc


def test_codelab_tool_selection_filters_mcp_tools(monkeypatch):
    _patch_registry_defaults(monkeypatch)
    monkeypatch.setattr(agent_tools.settings, "tool_selection_enabled", True)
    monkeypatch.setattr(agent_tools.settings, "tool_selection_fallback_tools", "datetime,calculator")

    registry = agent_tools.ToolRegistry(db=object(), user_id=1, route_profile="codelab")
    registry._mcp_tools = {
        "mcp.web.fetch": SimpleNamespace(
            name="mcp.web.fetch",
            description="web search fetch tool",
            parameters={"type": "object", "properties": {}},
        ),
        "mcp.code.exec": SimpleNamespace(
            name="mcp.code.exec",
            description="python code execute tool",
            parameters={"type": "object", "properties": {}},
        ),
    }

    selected = set(registry.select_tool_names_for_intent("web_query", user_text="latest news"))
    assert "mcp.web.fetch" in selected
    assert "mcp.code.exec" not in selected


def test_chat_registry_ignores_intent_filtering_and_exposes_full_pool(monkeypatch):
    _patch_registry_defaults(monkeypatch)
    monkeypatch.setattr(agent_tools.settings, "tool_selection_enabled", True)
    monkeypatch.setattr(agent_tools.settings, "tool_selection_fallback_tools", "datetime,calculator")

    registry = agent_tools.ToolRegistry(db=object(), user_id=1, route_profile="chat")
    registry._mcp_tools = {
        "mcp.firecrawl.firecrawl_scrape": SimpleNamespace(
            name="mcp.firecrawl.firecrawl_scrape",
            description="browser scrape tool",
            parameters={"type": "object", "properties": {}},
        ),
        "mcp.code.exec": SimpleNamespace(
            name="mcp.code.exec",
            description="python code execute tool",
            parameters={"type": "object", "properties": {}},
        ),
    }

    selected = set(registry.select_tool_names_for_intent("code_task", user_text="实现这个的最小代码是多少"))
    assert {
        "knowledge_search",
        "web_search",
        "calculator",
        "datetime",
        "text_analysis",
        "unit_converter",
        "literature_search",
        "mcp.firecrawl.firecrawl_scrape",
        "mcp.code.exec",
    }.issubset(selected)

    listed = {
        item["function"]["name"]
        for item in registry.list_tools(intent="web_query", user_text="latest news")
    }
    assert listed == selected


def test_tool_selection_classify_uploaded_pdf_as_knowledge_query():
    assert (
        agent_tools.ToolRegistry.classify_intent(
            "Please summarize my uploaded PDF and answer from my document."
        )
        == "knowledge_query"
    )
    assert agent_tools.ToolRegistry.classify_intent("根据我上传的文档做一个摘要") == "knowledge_query"


def test_notebook_uploaded_file_task_prefers_code_tools_and_skips_mcp(monkeypatch):
    monkeypatch.setattr(agent_tools.settings, "tool_selection_enabled", True)
    monkeypatch.setattr(agent_tools.settings, "tool_selection_fallback_tools", "datetime,calculator")

    class _Provider:
        def build_default_tools(self, ctx):
            return [
                _fake_tool("calculator"),
                _fake_tool("datetime"),
                _fake_tool("text_analysis"),
                _fake_tool("unit_converter"),
                _fake_tool("knowledge_search"),
            ]

        def build_notebook_tools(self, ctx):
            return [
                _fake_tool("notebook_execute"),
                _fake_tool("notebook_variables"),
                _fake_tool("notebook_cell"),
                _fake_tool("notebook_cleanup"),
                _fake_tool("pip_install"),
                _fake_tool("code_analysis"),
            ]

    registry = agent_tools.ToolRegistry(
        db=object(),
        user_id=1,
        notebook_id="nb-1",
        kernel_manager=object(),
        notebooks_store={},
        user_authorized=True,
        tool_provider=_Provider(),
    )
    registry._mcp_tools = {
        "mcp.tavily.tavily_search": SimpleNamespace(
            name="mcp.tavily.tavily_search",
            description="web search fetch tool",
            parameters={"type": "object", "properties": {}},
        ),
        "mcp.firecrawl.firecrawl_scrape": SimpleNamespace(
            name="mcp.firecrawl.firecrawl_scrape",
            description="browser scrape tool",
            parameters={"type": "object", "properties": {}},
        ),
    }

    user_text = "请根据我上传的 csv 文件在 notebook 里构建一个机器学习案例并画图"
    assert registry.resolve_intent(user_text) == "code_task"

    selected = set(registry.select_tool_names_for_intent("knowledge_query", user_text=user_text))
    assert "notebook_execute" in selected
    assert "notebook_cell" in selected
    assert "knowledge_search" not in selected
    assert "mcp.tavily.tavily_search" not in selected
    assert "mcp.firecrawl.firecrawl_scrape" not in selected


def test_codelab_route_profile_forces_uploaded_dataset_task_into_code_tools(monkeypatch):
    monkeypatch.setattr(agent_tools.settings, "tool_selection_enabled", True)
    monkeypatch.setattr(agent_tools.settings, "tool_selection_fallback_tools", "datetime,calculator,knowledge_search")

    class _Provider:
        def build_default_tools(self, ctx):
            return [
                _fake_tool("calculator"),
                _fake_tool("datetime"),
                _fake_tool("text_analysis"),
                _fake_tool("unit_converter"),
                _fake_tool("knowledge_search"),
                _fake_tool("web_search"),
                _fake_tool("web_scrape"),
            ]

        def build_notebook_tools(self, ctx):
            return [
                _fake_tool("notebook_execute"),
                _fake_tool("notebook_variables"),
                _fake_tool("notebook_cell"),
                _fake_tool("notebook_cleanup"),
                _fake_tool("pip_install"),
                _fake_tool("code_analysis"),
            ]

    registry = agent_tools.ToolRegistry(
        db=object(),
        user_id=1,
        notebook_id="nb-2",
        kernel_manager=object(),
        notebooks_store={},
        user_authorized=True,
        tool_provider=_Provider(),
        route_profile="codelab",
    )

    user_text = "请基于已上传的 car_parts_final.csv，先不要修改 notebook，也不要联网；只告诉我接下来最合理的两步。"
    assert registry.resolve_intent(user_text) == "code_task"

    selected = set(registry.select_tool_names_for_intent("web_query", user_text=user_text))
    assert "notebook_execute" in selected
    assert "notebook_variables" in selected
    assert "knowledge_search" not in selected
    assert "web_search" not in selected
    assert "web_scrape" not in selected


def test_codelab_followup_only_message_stays_in_code_task(monkeypatch):
    monkeypatch.setattr(agent_tools.settings, "tool_selection_enabled", True)
    monkeypatch.setattr(agent_tools.settings, "tool_selection_fallback_tools", "datetime,calculator,knowledge_search")

    class _Provider:
        def build_default_tools(self, ctx):
            return [
                _fake_tool("calculator"),
                _fake_tool("datetime"),
                _fake_tool("text_analysis"),
                _fake_tool("unit_converter"),
                _fake_tool("knowledge_search"),
                _fake_tool("web_search"),
                _fake_tool("web_scrape"),
            ]

        def build_notebook_tools(self, ctx):
            return [
                _fake_tool("notebook_execute"),
                _fake_tool("notebook_variables"),
                _fake_tool("notebook_cell"),
                _fake_tool("notebook_cleanup"),
                _fake_tool("pip_install"),
                _fake_tool("code_analysis"),
            ]

    registry = agent_tools.ToolRegistry(
        db=object(),
        user_id=1,
        notebook_id="nb-followup",
        kernel_manager=object(),
        notebooks_store={},
        user_authorized=True,
        tool_provider=_Provider(),
        route_profile="codelab",
    )

    user_text = "continue"
    assert registry.resolve_intent(user_text) == "code_task"

    selected = set(registry.select_tool_names_for_intent("general_chat", user_text=user_text))
    assert "notebook_execute" in selected
    assert "notebook_variables" in selected
    assert "knowledge_search" not in selected
    assert "web_search" not in selected


def test_codelab_explicit_web_request_adds_web_tools_without_dropping_notebook_tools(monkeypatch):
    monkeypatch.setattr(agent_tools.settings, "tool_selection_enabled", True)
    monkeypatch.setattr(agent_tools.settings, "tool_selection_fallback_tools", "datetime,calculator")

    class _Provider:
        def build_default_tools(self, ctx):
            return [
                _fake_tool("calculator"),
                _fake_tool("datetime"),
                _fake_tool("text_analysis"),
                _fake_tool("unit_converter"),
                _fake_tool("web_search"),
                _fake_tool("web_scrape"),
            ]

        def build_notebook_tools(self, ctx):
            return [
                _fake_tool("notebook_execute"),
                _fake_tool("notebook_variables"),
                _fake_tool("notebook_cell"),
                _fake_tool("notebook_cleanup"),
                _fake_tool("pip_install"),
                _fake_tool("code_analysis"),
            ]

    registry = agent_tools.ToolRegistry(
        db=object(),
        user_id=1,
        notebook_id="nb-explicit-web",
        kernel_manager=object(),
        notebooks_store={},
        user_authorized=True,
        tool_provider=_Provider(),
        route_profile="codelab",
    )

    selected = set(registry.select_tool_names_for_user_text("先查看当前 notebook，再联网搜一下这个报错"))
    assert "notebook_cell" in selected
    assert "notebook_variables" in selected
    assert "web_search" in selected
    assert "web_scrape" in selected


def test_codelab_default_selection_without_authorization_keeps_read_only_notebook_tools(monkeypatch):
    monkeypatch.setattr(agent_tools.settings, "tool_selection_enabled", True)
    monkeypatch.setattr(agent_tools.settings, "tool_selection_fallback_tools", "datetime,calculator")

    class _Provider:
        def build_default_tools(self, ctx):
            return [
                _fake_tool("calculator"),
                _fake_tool("datetime"),
                _fake_tool("text_analysis"),
                _fake_tool("unit_converter"),
            ]

        def build_notebook_tools(self, ctx):
            return [
                _fake_tool("notebook_execute"),
                _fake_tool("notebook_variables"),
                _fake_tool("notebook_cell"),
                _fake_tool("notebook_cleanup"),
                _fake_tool("pip_install"),
                _fake_tool("code_analysis"),
            ]

    registry = agent_tools.ToolRegistry(
        db=object(),
        user_id=1,
        notebook_id="nb-general-chat",
        kernel_manager=object(),
        notebooks_store={},
        user_authorized=False,
        tool_provider=_Provider(),
        route_profile="codelab",
    )

    selected = set(registry.select_tool_names_for_user_text("你好"))
    assert "datetime" in selected
    assert "calculator" in selected
    assert "notebook_execute" not in selected
    assert "notebook_variables" in selected
    assert "notebook_cell" in selected
    assert "code_analysis" in selected


def test_codelab_code_task_without_authorization_strips_mutation_tools(monkeypatch):
    monkeypatch.setattr(agent_tools.settings, "tool_selection_enabled", True)
    monkeypatch.setattr(agent_tools.settings, "tool_selection_fallback_tools", "datetime,calculator")

    class _Provider:
        def build_default_tools(self, ctx):
            return [
                _fake_tool("calculator"),
                _fake_tool("datetime"),
                _fake_tool("text_analysis"),
                _fake_tool("unit_converter"),
            ]

        def build_notebook_tools(self, ctx):
            return [
                _fake_tool("notebook_execute"),
                _fake_tool("notebook_variables"),
                _fake_tool("notebook_cell"),
                _fake_tool("notebook_cleanup"),
                _fake_tool("pip_install"),
                _fake_tool("code_analysis"),
            ]

    registry = agent_tools.ToolRegistry(
        db=object(),
        user_id=1,
        notebook_id="nb-unauthorized",
        kernel_manager=object(),
        notebooks_store={},
        user_authorized=False,
        tool_provider=_Provider(),
        route_profile="codelab",
    )

    user_text = "根据当前 notebook 和上传文件，告诉我下一步怎么做"
    selected = set(registry.select_tool_names_for_intent("code_task", user_text=user_text))

    assert "notebook_variables" in selected
    assert "notebook_cell" in selected
    assert "code_analysis" in selected
    assert "notebook_execute" not in selected
    assert "notebook_cleanup" not in selected
    assert "pip_install" not in selected


def test_codelab_negative_web_instruction_keeps_current_notebook_prompt_local(monkeypatch):
    monkeypatch.setattr(agent_tools.settings, "tool_selection_enabled", True)
    monkeypatch.setattr(agent_tools.settings, "tool_selection_fallback_tools", "datetime,calculator,knowledge_search")

    class _Provider:
        def build_default_tools(self, ctx):
            return [
                _fake_tool("calculator"),
                _fake_tool("datetime"),
                _fake_tool("text_analysis"),
                _fake_tool("unit_converter"),
                _fake_tool("knowledge_search"),
                _fake_tool("web_search"),
                _fake_tool("web_scrape"),
            ]

        def build_notebook_tools(self, ctx):
            return [
                _fake_tool("notebook_execute"),
                _fake_tool("notebook_variables"),
                _fake_tool("notebook_cell"),
                _fake_tool("notebook_cleanup"),
                _fake_tool("pip_install"),
                _fake_tool("code_analysis"),
            ]

    registry = agent_tools.ToolRegistry(
        db=object(),
        user_id=1,
        notebook_id="nb-local-only",
        kernel_manager=object(),
        notebooks_store={},
        user_authorized=True,
        tool_provider=_Provider(),
        route_profile="codelab",
    )

    user_text = "只根据当前 notebook 状态，简短告诉我下一步应该做什么，不要执行，不要联网。"
    assert registry.resolve_intent(user_text) == "code_task"

    selected = set(registry.select_tool_names_for_intent(registry.resolve_intent(user_text), user_text=user_text))
    assert "notebook_cell" in selected
    assert "notebook_variables" in selected
    assert "web_search" not in selected
    assert "web_scrape" not in selected


class _FakeSoup:
    def __init__(self, html: str, parser: str):
        self._html = html

    def __call__(self, tags):
        return []

    def get_text(self, separator="\n", strip=True):
        return "hello world"

    def prettify(self):
        return self._html

    def find(self, name):
        return None

    def find_all(self, name, **kwargs):
        return []

    def select(self, selector):
        return []


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        return None


class _FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str):
        return _FakeResponse("<html><body>Hello</body></html>")


@pytest.mark.asyncio
async def test_web_scrape_blocks_when_robots_disallow(monkeypatch):
    tool = notebook_tools.WebScrapeTool()
    monkeypatch.setattr(notebook_tools, "BS4_AVAILABLE", True)

    async def _deny(*args, **kwargs):
        return False, "https://example.com/robots.txt"

    async def _no_rate(*args, **kwargs):
        return None

    monkeypatch.setattr(tool, "_check_robots", _deny)
    monkeypatch.setattr(tool, "_check_domain_rate_limit", _no_rate)

    result = await tool.execute(url="https://example.com/page", extract="text")
    assert result.success is False
    assert result.error == "robots_disallowed"


@pytest.mark.asyncio
async def test_web_scrape_blocks_when_rate_limited(monkeypatch):
    tool = notebook_tools.WebScrapeTool()
    monkeypatch.setattr(notebook_tools, "BS4_AVAILABLE", True)

    async def _allow(*args, **kwargs):
        return True, None

    async def _limit(*args, **kwargs):
        return 0.75

    monkeypatch.setattr(tool, "_check_robots", _allow)
    monkeypatch.setattr(tool, "_check_domain_rate_limit", _limit)

    result = await tool.execute(url="https://example.com/page", extract="text")
    assert result.success is False
    assert result.error == "rate_limited"
    assert result.data["retry_after_seconds"] == 0.75


@pytest.mark.asyncio
async def test_web_scrape_still_returns_content_when_allowed(monkeypatch):
    tool = notebook_tools.WebScrapeTool()
    monkeypatch.setattr(notebook_tools, "BS4_AVAILABLE", True)
    monkeypatch.setattr(notebook_tools, "BeautifulSoup", _FakeSoup, raising=False)
    monkeypatch.setattr(notebook_tools.httpx, "AsyncClient", lambda **kwargs: _FakeClient())

    async def _allow(*args, **kwargs):
        return True, None

    async def _no_rate(*args, **kwargs):
        return None

    monkeypatch.setattr(tool, "_check_robots", _allow)
    monkeypatch.setattr(tool, "_check_domain_rate_limit", _no_rate)

    result = await tool.execute(url="https://example.com/page", extract="text")
    assert result.success is True
    assert "hello world" in result.output


def _paper(idx: int, *, doi: str | None = None):
    return SimpleNamespace(
        source="semantic_scholar",
        external_id=f"id-{idx}",
        title=f"title-{idx}",
        abstract="abstract",
        authors=[{"name": "a"}],
        year=2024,
        venue="venue",
        citation_count=idx,
        reference_count=0,
        url=f"https://example.com/{idx}",
        pdf_url=None,
        arxiv_id=None,
        doi=doi,
        fields_of_study=[],
    )


@pytest.mark.asyncio
async def test_literature_search_multi_and_default_source_compatible():
    class _FakeService:
        def __init__(self):
            self.multi_calls = 0
            self.search_calls = 0
            self.last_search_source = None

        async def search_multi(self, **kwargs):
            self.multi_calls += 1
            return {"total": 2, "papers": [_paper(1), _paper(2)]}

        async def search(self, **kwargs):
            self.search_calls += 1
            self.last_search_source = kwargs.get("source")
            return {"total": 1, "papers": [_paper(3)]}

        def multi_source_count(self):
            return 4

    service = _FakeService()
    tool = agent_tools.LiteratureSearchTool()
    tool.service = service

    multi_result = await tool.execute(query="transformer", source="multi", max_results=1)
    default_result = await tool.execute(query="transformer", max_results=1)

    assert multi_result.success is True
    assert multi_result.data["source"] == "multi"
    assert len(multi_result.data["papers"]) == 1
    assert default_result.success is True
    assert default_result.data["source"] == "auto"
    assert service.last_search_source == "auto"
    assert service.multi_calls == 1
    assert service.search_calls == 1


@pytest.mark.asyncio
async def test_notebook_literature_tool_supports_multi():
    class _FakeService:
        async def search_multi(self, **kwargs):
            return {"total": 1, "papers": [_paper(1)]}

        async def search(self, **kwargs):
            return {"total": 1, "papers": [_paper(2)]}

        def multi_source_count(self):
            return 4

    tool = notebook_tools.EnhancedLiteratureSearchTool()
    tool.service = _FakeService()

    result = await tool.execute(query="rag", source="multi", max_results=1)
    assert result.success is True
    assert result.data["source"] == "multi"


@pytest.mark.asyncio
async def test_literature_service_multi_deduplicates_and_keeps_better_paper():
    from app.services.literature_service import LiteratureService, PaperResult

    def _paper_result(*, source: str, external_id: str, doi: str, citation_count: int) -> PaperResult:
        return PaperResult(
            source=source,
            external_id=external_id,
            title="Same Title",
            abstract="abstract",
            authors=[{"name": "a"}],
            year=2024,
            venue="venue",
            citation_count=citation_count,
            reference_count=0,
            url=f"https://example.com/{external_id}",
            pdf_url=None,
            arxiv_id=None,
            doi=doi,
            fields_of_study=[],
            raw_data={},
        )

    service = LiteratureService()

    async def _s2(*args, **kwargs):
        return {"papers": [_paper_result(source="semantic_scholar", external_id="s2-1", doi="10.1000/abc", citation_count=1)]}

    async def _arxiv(*args, **kwargs):
        return {"papers": []}

    async def _pubmed(*args, **kwargs):
        return {"papers": [_paper_result(source="pubmed", external_id="pm-1", doi="10.1000/abc", citation_count=10)]}

    async def _openalex(*args, **kwargs):
        return {"papers": []}

    service.s2.search = _s2
    service.arxiv.search = _arxiv
    service.pubmed.search = _pubmed
    service.openalex.search = _openalex

    result = await service.search_multi("transformer", limit_per_source=3)
    papers = result["papers"]

    assert result["total"] == 1
    assert len(papers) == 1
    assert papers[0].source == "pubmed"
    assert papers[0].citation_count == 10


@pytest.mark.asyncio
async def test_literature_service_auto_fallback_tries_multiple_sources_until_success():
    from app.services.literature_service import LiteratureService, PaperResult

    def _paper_result(*, source: str, external_id: str) -> PaperResult:
        return PaperResult(
            source=source,
            external_id=external_id,
            title="Recovered Paper",
            abstract="abstract",
            authors=[{"name": "a"}],
            year=2024,
            venue="venue",
            citation_count=1,
            reference_count=0,
            url=f"https://example.com/{external_id}",
            pdf_url=None,
            arxiv_id=None,
            doi=None,
            fields_of_study=[],
            raw_data={},
        )

    service = LiteratureService()
    calls = []

    async def _openalex(*args, **kwargs):
        calls.append("openalex")
        return {"papers": [], "error": "rate_limited"}

    async def _s2(*args, **kwargs):
        calls.append("semantic_scholar")
        return {"papers": []}

    async def _arxiv(*args, **kwargs):
        calls.append("arxiv")
        return {"papers": [_paper_result(source="arxiv", external_id="ax-1")], "total": 1}

    service.openalex.search = _openalex
    service.s2.search = _s2
    service.arxiv.search = _arxiv
    service.pubmed.search = _s2
    service.crossref.search = _s2

    result = await service.search("transformer", source="auto", limit=3)

    assert result["resolved_source"] == "arxiv"
    assert result["attempted_sources"] == ["openalex", "semantic_scholar", "arxiv"]
    assert result["partial_errors"] == {"openalex": "rate_limited"}
    assert calls == ["openalex", "semantic_scholar", "arxiv"]
