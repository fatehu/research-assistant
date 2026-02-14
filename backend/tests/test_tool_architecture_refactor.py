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
async def test_web_search_prefers_serper_when_available(monkeypatch):
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
    assert result.output == "serper ok"
    assert calls == ["serper"]


@pytest.mark.asyncio
async def test_web_search_fallback_chain(monkeypatch):
    tool = agent_tools.WebSearchTool()
    tool.serper_api_key = "x"
    tool.tavily_api_key = "y"
    calls = []

    async def _serper(*args, **kwargs):
        calls.append("serper")
        return _tool_result(False, "serper failed", error="serper_down")

    async def _tavily(*args, **kwargs):
        calls.append("tavily")
        return _tool_result(False, "tavily failed", error="tavily_down")

    async def _ddgs(*args, **kwargs):
        calls.append("ddgs")
        return _tool_result(True, "ddgs ok")

    monkeypatch.setattr(tool, "_serper_search", _serper)
    monkeypatch.setattr(tool, "_tavily_search", _tavily)
    monkeypatch.setattr(tool, "_ddgs_search", _ddgs)

    result = await tool._execute("q", max_results=3)
    assert result.success is True
    assert result.output == "ddgs ok"
    assert calls == ["serper", "tavily", "ddgs"]


@pytest.mark.asyncio
async def test_web_search_fallback_chain_when_provider_raises_request_error(monkeypatch):
    tool = agent_tools.WebSearchTool()
    tool.serper_api_key = "x"
    tool.tavily_api_key = "y"
    calls = []

    async def _serper(*args, **kwargs):
        calls.append("serper")
        raise httpx.ConnectError(
            "dns failed",
            request=httpx.Request("POST", "https://google.serper.dev/search"),
        )

    async def _tavily(*args, **kwargs):
        calls.append("tavily")
        return _tool_result(False, "tavily failed", error="tavily_down")

    async def _ddgs(*args, **kwargs):
        calls.append("ddgs")
        return _tool_result(True, "ddgs ok")

    monkeypatch.setattr(tool, "_serper_search", _serper)
    monkeypatch.setattr(tool, "_tavily_search", _tavily)
    monkeypatch.setattr(tool, "_ddgs_search", _ddgs)

    result = await tool._execute("q", max_results=3)
    assert result.success is True
    assert result.output == "ddgs ok"
    assert calls == ["serper", "tavily", "ddgs"]


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


def test_tool_selection_filters_tools_and_keeps_fallback(monkeypatch):
    _patch_registry_defaults(monkeypatch)
    monkeypatch.setattr(agent_tools.settings, "tool_selection_enabled", True)
    monkeypatch.setattr(agent_tools.settings, "tool_selection_fallback_tools", "datetime,calculator")

    registry = agent_tools.ToolRegistry(db=object(), user_id=1)
    selected = set(registry.select_tool_names_for_intent("knowledge_query"))

    assert {"knowledge_search", "datetime", "calculator"}.issubset(selected)
    assert "web_search" not in selected

    desc = registry.get_tools_description(intent="web_query", user_text="today latest news")
    assert "web_search" in desc
    assert "datetime" in desc
    assert "calculator" in desc
    assert "knowledge_search" not in desc


def test_tool_selection_filters_mcp_tools(monkeypatch):
    _patch_registry_defaults(monkeypatch)
    monkeypatch.setattr(agent_tools.settings, "tool_selection_enabled", True)
    monkeypatch.setattr(agent_tools.settings, "tool_selection_fallback_tools", "datetime,calculator")

    registry = agent_tools.ToolRegistry(db=object(), user_id=1)
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


def test_tool_selection_classify_uploaded_pdf_as_knowledge_query():
    assert (
        agent_tools.ToolRegistry.classify_intent(
            "Please summarize my uploaded PDF and answer from my document."
        )
        == "knowledge_query"
    )
    assert agent_tools.ToolRegistry.classify_intent("根据我上传的文档做一个摘要") == "knowledge_query"


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

        async def search_multi(self, **kwargs):
            self.multi_calls += 1
            return {"total": 2, "papers": [_paper(1), _paper(2)]}

        async def search(self, **kwargs):
            self.search_calls += 1
            return {"total": 1, "papers": [_paper(3)]}

    service = _FakeService()
    tool = agent_tools.LiteratureSearchTool()
    tool.service = service

    multi_result = await tool.execute(query="transformer", source="multi", max_results=1)
    default_result = await tool.execute(query="transformer", source="semantic_scholar", max_results=1)

    assert multi_result.success is True
    assert multi_result.data["source"] == "multi"
    assert len(multi_result.data["papers"]) == 1
    assert default_result.success is True
    assert default_result.data["source"] == "semantic_scholar"
    assert service.multi_calls == 1
    assert service.search_calls == 1


@pytest.mark.asyncio
async def test_notebook_literature_tool_supports_multi():
    class _FakeService:
        async def search_multi(self, **kwargs):
            return {"total": 1, "papers": [_paper(1)]}

        async def search(self, **kwargs):
            return {"total": 1, "papers": [_paper(2)]}

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

    service.s2.search = _s2
    service.arxiv.search = _arxiv
    service.pubmed.search = _pubmed

    result = await service.search_multi("transformer", limit_per_source=3)
    papers = result["papers"]

    assert result["total"] == 1
    assert len(papers) == 1
    assert papers[0].source == "pubmed"
    assert papers[0].citation_count == 10
