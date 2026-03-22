import os
import sys
import json
import inspect
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse, StreamingResponse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api import literature as literature_api
from app.models.knowledge import DocumentStatus
from app.models.literature import KnowledgeLinkStatus
from app.services import agent_tools
from app.services.generative_reader_agent_runtime import GenerativeReaderAgentRuntime
from app.services.literature_reader_compose_service import LiteratureReaderComposeService
from app.services.mcp.client import MCPClientManager, MCPToolSchema


class _FakeResult:
    def __init__(self, *, row=None, rows=None):
        self._row = row
        self._rows = list(rows or [])

    def scalar_one_or_none(self):
        return self._row

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.committed = False

    async def execute(self, _query):
        if not self._results:
            return _FakeResult(rows=[])
        return self._results.pop(0)

    async def commit(self):
        self.committed = True


class _CountingTool(agent_tools.Tool):
    def __init__(self, name: str, output: str | None = None):
        self.name = name
        self.description = f"{name} desc"
        self.parameters = {"type": "object", "properties": {}}
        self.calls: list[dict[str, object]] = []
        self.output = output or f"{name} local"

    async def execute(self, **kwargs):
        self.calls.append(dict(kwargs))
        return agent_tools.ToolResult(success=True, output=self.output, data={"kwargs": kwargs})


class _FakeRoutedMCPManager:
    def __init__(self, schemas: list[MCPToolSchema], responses: dict[str, object]):
        self._schemas = {schema.qualified_name: schema for schema in schemas}
        self._responses = dict(responses)
        self.call_history: list[tuple[str, dict[str, object]]] = []

    async def discover_tools(self, force_refresh: bool = False):
        return list(self._schemas.values())

    def resolve_tool_schema(self, name: str):
        return self._schemas.get(name)

    async def call_tool(self, tool_name: str, arguments: dict[str, object]):
        self.call_history.append((tool_name, dict(arguments)))
        value = self._responses.get(tool_name)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return SimpleNamespace(success=False, output=f"missing:{tool_name}", data=None, error="tool_not_found")
        return value


def test_is_scaffold_like_generative_plan_should_accept_planner_timeout_fallback():
    runtime = GenerativeReaderAgentRuntime()
    compose_payload = {
        "enrichment_bundle": {
            "version": "v1",
            "targets": [
                {
                    "target_id": "p7:fig-1",
                    "target_kind": "figure",
                    "component_type": "FigurePanel",
                    "title": "Fig 3",
                    "excerpt": "Concordance and insight of ChatGPT on USMLE.",
                    "figure_label": "Fig 3",
                },
                {
                    "target_id": "p7:p-1",
                    "target_kind": "paragraph",
                    "component_type": "ParagraphProse",
                    "excerpt": "We first examined the frequency of insight.",
                    "section_label": "Results",
                },
            ],
            "resource_modules": [],
            "interaction_modules": [],
            "meta": {},
        }
    }
    fallback = runtime._build_fallback_plan(  # pylint: disable=protected-access
        page=7,
        user_intent="Create a paper experience",
        enrichment_bundle=compose_payload["enrichment_bundle"],
    )
    fallback["meta"]["fallback_reason"] = "planner_timeout"
    finalized = runtime._finalize_plan(  # pylint: disable=protected-access
        parsed=fallback,
        page=7,
        user_intent="Create a paper experience",
        enrichment_bundle=compose_payload["enrichment_bundle"],
        compose_payload=compose_payload,
        used_tools=[],
        tool_trace=[],
    )

    assert literature_api._is_scaffold_like_generative_plan(finalized) is False


def _patch_registry_default_tools(
    monkeypatch: pytest.MonkeyPatch,
    *,
    web_search_tool: _CountingTool | None = None,
    web_scrape_tool: _CountingTool | None = None,
):
    monkeypatch.setattr(agent_tools, "WebSearchTool", lambda: web_search_tool or _CountingTool("web_search"))
    monkeypatch.setattr(agent_tools, "WebScrapeTool", lambda: web_scrape_tool or _CountingTool("web_scrape"))
    monkeypatch.setattr(agent_tools, "CalculatorTool", lambda: _CountingTool("calculator"))
    monkeypatch.setattr(agent_tools, "DateTimeTool", lambda: _CountingTool("datetime"))
    monkeypatch.setattr(agent_tools, "TextAnalysisTool", lambda: _CountingTool("text_analysis"))
    monkeypatch.setattr(agent_tools, "UnitConverterTool", lambda: _CountingTool("unit_converter"))
    monkeypatch.setattr(agent_tools, "LiteratureSearchTool", lambda: _CountingTool("literature_search"))


@pytest.mark.asyncio
async def test_get_reader_composed_page_cached_should_repair_malformed_fallback_payload(monkeypatch):
    paper = SimpleNamespace(id=85, title="Fallback Paper", pdf_path="demo.pdf")
    real_service = LiteratureReaderComposeService()

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeComposeService:
        async def get_latest_cached_payload_only(self, **_kwargs):
            return {
                "paper_id": 85,
                "page": 1,
                "status": "fallback",
                "degraded_reason": "no_drop_blocks_failed_auto_fallback",
                "pipeline_version": "simplified_v2",
                "source_signature": "legacy-fallback-sig",
                "build_mode": "compose_agent_simplified",
                "ui_plan": {
                    "components": [],
                    "layout": {},
                    "style_tokens": {},
                    "trace_meta": {},
                },
                "quality_report": {
                    "overall": 0.0,
                    "degraded": True,
                    "stop_reason": "no_drop_blocks_failed_auto_fallback",
                },
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }

        def _ensure_payload_contract(self, *, page: int, payload: dict):
            return real_service._ensure_payload_contract(page=page, payload=payload)

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())

    response = await literature_api.get_reader_composed_page_cached(
        paper_id=85,
        payload=literature_api.ReaderComposeRequest(page=1, selected_kb_id=84),
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=1),
    )

    assert response.payload.status == "fallback"
    assert response.payload.engine_version
    assert response.payload.ui_plan.plan_id
    assert response.payload.build_mode == "compose_agent_simplified"
    assert response.cache_meta["cache_layer"] == "db"


@pytest.mark.asyncio
async def test_get_reader_composed_page_cached_should_pass_pipeline_version_override(monkeypatch):
    paper = SimpleNamespace(id=78, title="Demo Paper", pdf_path="demo.pdf")
    captured: dict[str, object] = {}

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeComposeService:
        async def get_latest_cached_payload_only(self, **kwargs):
            captured.update(kwargs)
            return {
                "paper_id": 78,
                "page": 7,
                "status": "done",
                "pipeline_version": "layout_uid_v1",
                "engine_version": "reader_compose_v6",
                "source_signature": "layout-uid-sig",
                "build_mode": "compose_agent_layout_uid_v1",
                "ui_plan": {
                    "plan_id": "layout_uid_v1_p7",
                    "components": [],
                    "layout": {},
                    "style_tokens": {},
                    "trace_meta": {},
                },
                "quality_report": {"overall": 0.91, "degraded": False, "stop_reason": "layout_uid_v1_done"},
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }

        def _ensure_payload_contract(self, *, page: int, payload: dict):
            return payload

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())

    response = await literature_api.get_reader_composed_page_cached(
        paper_id=78,
        payload=literature_api.ReaderComposeRequest(page=7, pipeline_version="layout_uid_v1"),
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=1),
    )

    assert captured["pipeline_version_override"] == "layout_uid_v1"
    assert response.payload.pipeline_version == "layout_uid_v1"


@pytest.mark.asyncio
async def test_get_reader_composed_page_cached_should_default_to_layout_uid_v1_pipeline(monkeypatch):
    paper = SimpleNamespace(id=78, title="Demo Paper", pdf_path="demo.pdf")
    captured: dict[str, object] = {}

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeComposeService:
        async def get_latest_cached_payload_only(self, **kwargs):
            captured.update(kwargs)
            return {
                "paper_id": 78,
                "page": 7,
                "status": "done",
                "pipeline_version": "layout_uid_v1",
                "engine_version": "reader_compose_v6",
                "source_signature": "layout-uid-default-sig",
                "build_mode": "compose_agent_layout_uid_v1",
                "ui_plan": {
                    "plan_id": "layout_uid_v1_p7",
                    "components": [],
                    "layout": {},
                    "style_tokens": {},
                    "trace_meta": {},
                },
                "quality_report": {"overall": 0.92, "degraded": False, "stop_reason": "layout_uid_v1_done"},
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }

        def _ensure_payload_contract(self, *, page: int, payload: dict):
            return payload

    monkeypatch.setattr(literature_api.settings, "reader_pipeline_version", "layout_uid_v1")
    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())

    response = await literature_api.get_reader_composed_page_cached(
        paper_id=78,
        payload=literature_api.ReaderComposeRequest(page=7),
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=1),
    )

    assert captured["pipeline_version_override"] is None
    assert response.payload.pipeline_version == "layout_uid_v1"


@pytest.mark.asyncio
async def test_stream_paper_pdf_reads_local_file(monkeypatch, tmp_path: Path):
    pdf_path = tmp_path / "paper_10.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%EOF\n")

    paper = SimpleNamespace(id=10, title="Test Paper", pdf_path=str(pdf_path), pdf_url="https://example.com/a.pdf")

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)

    response = await literature_api.stream_paper_pdf(
        paper_id=10,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=99),
    )

    assert isinstance(response, FileResponse)
    assert Path(response.path) == pdf_path
    assert response.headers.get("content-disposition", "").startswith("inline;")


@pytest.mark.asyncio
async def test_stream_paper_pdf_raises_404_when_missing(monkeypatch):
    paper = SimpleNamespace(id=11, title="Missing Paper", pdf_path=None, pdf_url="https://example.com/missing.pdf")

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)

    with pytest.raises(HTTPException) as exc:
        await literature_api.stream_paper_pdf(
            paper_id=11,
            db=SimpleNamespace(),
            current_user=SimpleNamespace(id=99),
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_stream_reader_docmind_page_image_reads_local_file(monkeypatch, tmp_path: Path):
    image_path = tmp_path / "docmind_page.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    paper = SimpleNamespace(id=85, user_id=1)

    class _FakeDBWithPaper:
        async def get(self, _model, _paper_id):
            return paper

    class _FakeReaderService:
        async def build_or_get_page_payload(self, **_kwargs):
            return {
                "docmind_structure": {
                    "page_image_path": str(image_path),
                    "page_image_url": "",
                },
            }, SimpleNamespace()

    monkeypatch.setattr(literature_api, "get_literature_reader_service", lambda: _FakeReaderService())

    response = await literature_api.stream_reader_docmind_page_image(
        paper_id=85,
        page=7,
        db=_FakeDBWithPaper(),
    )

    assert isinstance(response, FileResponse)
    assert Path(response.path) == image_path


@pytest.mark.asyncio
async def test_stream_reader_grounding_page_asset_reads_localized_file(monkeypatch, tmp_path: Path):
    image_path = tmp_path / "grounding_pages" / "page_7.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    paper = SimpleNamespace(id=85, user_id=1)

    class _FakeDBWithPaper:
        async def get(self, _model, _paper_id):
            return paper

    class _FakeComposeService:
        @staticmethod
        def _find_existing_grounding_page_image_path(*, paper_id, page):  # pylint: disable=unused-argument
            return str(image_path)

    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())

    response = await literature_api.stream_reader_grounding_page_asset(
        paper_id=85,
        page=7,
        db=_FakeDBWithPaper(),
    )

    assert isinstance(response, FileResponse)
    assert Path(response.path) == image_path


@pytest.mark.asyncio
async def test_stream_reader_docmind_page_image_localizes_remote_url(monkeypatch, tmp_path: Path):
    paper = SimpleNamespace(id=85, user_id=1)

    class _FakeDBWithPaper:
        async def get(self, _model, _paper_id):
            return paper

    class _FakeReaderService:
        async def build_or_get_page_payload(self, **_kwargs):
            return {
                "docmind_structure": {
                    "page_image_path": "",
                    "page_image_url": "https://example.com/page.png",
                },
            }, SimpleNamespace()

    monkeypatch.setattr(literature_api, "get_literature_reader_service", lambda: _FakeReaderService())

    localized_path = tmp_path / "paper_85" / "grounding_pages" / "page_7.png"
    localized_path.parent.mkdir(parents=True, exist_ok=True)
    localized_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    class _FakeComposeService:
        @staticmethod
        def _ensure_local_grounding_page_image(*, paper_id, page, page_image_url, page_image_path):  # pylint: disable=unused-argument
            return str(localized_path)

    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())

    response = await literature_api.stream_reader_docmind_page_image(
        paper_id=85,
        page=7,
        db=_FakeDBWithPaper(),
    )

    assert isinstance(response, FileResponse)
    assert Path(response.path) == localized_path


@pytest.mark.asyncio
async def test_download_paper_pdf_derives_arxiv_pdf_url_from_doi(monkeypatch, tmp_path: Path):
    paper = SimpleNamespace(
        id=17,
        user_id=9,
        title="Attention Is All You Need",
        pdf_url=None,
        pdf_downloaded=False,
        pdf_path=None,
        arxiv_id=None,
        arxiv_url=None,
        doi="10.48550/arXiv.1706.03762",
        raw_data={"imported_link": "https://doi.org/10.48550/arXiv.1706.03762"},
        document_id=None,
    )
    db = _FakeDB([_FakeResult(row=paper)])
    captured: dict[str, str] = {}
    save_path = tmp_path / "attention_17.pdf"

    class _FakeService:
        async def download_pdf(self, pdf_url: str, path: str):
            captured["pdf_url"] = pdf_url
            captured["save_path"] = path
            Path(path).write_bytes(b"%PDF-1.4\n%EOF\n")
            return True, ""

    monkeypatch.setattr(literature_api, "get_literature_service", lambda: _FakeService())
    monkeypatch.setattr(
        literature_api,
        "_build_paper_pdf_file_path",
        lambda **kwargs: str(save_path),
    )

    response = await literature_api.download_paper_pdf(
        paper_id=17,
        knowledge_base_id=None,
        background_tasks=None,
        db=db,
        current_user=SimpleNamespace(id=9),
    )

    assert response["message"] == "PDF 下载成功"
    assert captured["pdf_url"] == "https://arxiv.org/pdf/1706.03762"
    assert captured["save_path"] == str(save_path)
    assert paper.pdf_downloaded is True
    assert paper.pdf_path == str(save_path)
    assert paper.pdf_url == "https://arxiv.org/pdf/1706.03762"
    assert save_path.exists()
    assert db.committed is True


@pytest.mark.asyncio
async def test_download_paper_pdf_falls_back_when_stored_pdf_url_is_stale(monkeypatch, tmp_path: Path):
    paper = SimpleNamespace(
        id=18,
        user_id=9,
        title="Attention Is All You Need",
        pdf_url="https://example.com/stale.pdf",
        pdf_downloaded=False,
        pdf_path=None,
        arxiv_id=None,
        arxiv_url=None,
        doi="10.48550/arXiv.1706.03762",
        raw_data={"imported_link": "https://doi.org/10.48550/arXiv.1706.03762"},
        document_id=None,
    )
    db = _FakeDB([_FakeResult(row=paper)])
    attempts: list[str] = []
    save_path = tmp_path / "attention_18.pdf"

    class _FakeService:
        async def download_pdf(self, pdf_url: str, path: str):
            attempts.append(pdf_url)
            if pdf_url == "https://example.com/stale.pdf":
                return False, "PDF 下载失败，上游返回 404"
            Path(path).write_bytes(b"%PDF-1.4\n%EOF\n")
            return True, ""

    monkeypatch.setattr(literature_api, "get_literature_service", lambda: _FakeService())
    monkeypatch.setattr(
        literature_api,
        "_build_paper_pdf_file_path",
        lambda **kwargs: str(save_path),
    )

    response = await literature_api.download_paper_pdf(
        paper_id=18,
        knowledge_base_id=None,
        background_tasks=None,
        db=db,
        current_user=SimpleNamespace(id=9),
    )

    assert response["message"] == "PDF 下载成功"
    assert attempts == [
        "https://arxiv.org/pdf/1706.03762",
    ]
    assert paper.pdf_url == "https://arxiv.org/pdf/1706.03762"


@pytest.mark.asyncio
async def test_create_reader_composed_review_session_forwards_cache_clone_flags(monkeypatch):
    paper = SimpleNamespace(id=78, title="Demo Paper", pdf_path="demo.pdf")
    captured: dict[str, object] = {}

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeService:
        async def create_review_session(self, **kwargs):
            captured.update(kwargs)
            return {
                "snapshot_id": "snapshot_fast",
                "session_id": "session_fast",
                "page": 7,
                "paper_id": 78,
                "source_signature": "sig-fast",
                "ui_plan": {"components": [], "layout": {}, "style_tokens": {}, "trace_meta": {}},
                "assets": [],
            }

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeService())

    payload = literature_api.ReaderComposeReviewSessionRequest(
        page=7,
        selected_kb_id=84,
        snapshot_label="snapshot_fast",
        prefer_cache_clone=True,
        allow_recompute_on_cache_miss=False,
    )

    response = await literature_api.create_reader_composed_review_session(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert response["snapshot_id"] == "snapshot_fast"
    assert captured["paper"] is paper
    assert captured["page"] == 7
    assert captured["prefer_cache_clone"] is True
    assert captured["allow_recompute_on_cache_miss"] is False


@pytest.mark.asyncio
async def test_create_reader_composed_review_session_returns_404_for_cache_miss(monkeypatch):
    paper = SimpleNamespace(id=78, title="Demo Paper", pdf_path="demo.pdf")

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeService:
        async def create_review_session(self, **_kwargs):
            raise ValueError("review_cache_not_found")

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeService())

    payload = literature_api.ReaderComposeReviewSessionRequest(
        page=7,
        prefer_cache_clone=True,
        allow_recompute_on_cache_miss=False,
    )

    with pytest.raises(HTTPException) as exc:
        await literature_api.create_reader_composed_review_session(
            paper_id=78,
            payload=payload,
            db=SimpleNamespace(),
            current_user=SimpleNamespace(id=5),
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "review_cache_not_found"


@pytest.mark.asyncio
async def test_get_reader_composed_generative_plan_should_build_from_compose_payload(monkeypatch):
    paper = SimpleNamespace(id=78, title="Demo Paper", pdf_path="demo.pdf")
    captured: dict[str, object] = {}

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeComposeService:
        async def build_or_get_composed_payload(self, **kwargs):
            captured["compose_kwargs"] = kwargs
            payload = {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "compose-sig",
                "scheme_choice": {"scheme_id": "reading_flow_stack", "label": "Reading Flow"},
                "enrichment_bundle": {
                    "version": "v1",
                    "targets": [
                        {
                            "target_id": "p7:paragraph_1",
                            "node_id": "paragraph_1",
                            "target_kind": "paragraph",
                            "component_type": "ParagraphProse",
                            "title": "Main body",
                            "excerpt": "excerpt",
                            "source_block_ids": ["p7_dm_p7_l009_b001"],
                            "source_atom_ids": [],
                            "section_label": "Results",
                            "figure_label": "",
                            "suggested_resource_types": ["related_public_resource"],
                        }
                    ],
                    "resource_modules": [],
                    "interaction_modules": [],
                    "meta": {},
                },
            }
            meta = SimpleNamespace(
                cache_hit=True,
                cache_layer="redis",
                build_mode="compose_agent_simplified",
                source_signature="compose-sig",
                source_sig_hash="sig-hash",
            )
            return payload, meta

    class _FakeRuntime:
        async def build_plan(self, **kwargs):
            captured["runtime_kwargs"] = kwargs
            return {
                "version": "v1",
                "status": "done",
                "shell_mode": "resource_augmented_reader",
                "rationale": ["Use public resources around the body paragraph."],
                "resource_modules": [],
                "interaction_modules": [],
                "js_widgets": [],
                "used_tools": ["paper_read"],
                "tool_trace": [],
                "meta": {"page": 7},
            }

    async def _fake_build_registry(**kwargs):
        captured["tool_registry_kwargs"] = kwargs
        return object(), {"paper_read", "knowledge_search", "web_search", "web_scrape"}

    async def _fake_adjacent_context(**kwargs):
        captured["adjacent_context_kwargs"] = kwargs
        return [
            {
                "page": 6,
                "relation": "previous_page",
                "reference_only": True,
                "source": "vlflash_page_ocr",
                "summary": "上一页承接摘要",
                "body_text": "上一页承接段落",
                "figures": [{"label": "Figure 1", "description": "上一页主图说明"}],
                "tables": [],
                "equations": [],
                "continuation_hints": ["当前页延续了上一页的图示解释。"],
            },
            {
                "page": 8,
                "relation": "next_page",
                "reference_only": True,
                "source": "vlflash_page_ocr",
                "summary": "下一页延续摘要",
                "body_text": "下一页延续段落",
                "figures": [],
                "tables": [{"label": "Table 2", "description": "下一页表格说明"}],
                "equations": [],
                "continuation_hints": ["当前页的结论在下一页继续展开。"],
            },
        ]

    async def _fake_plan_cache_get(_cache_key: str):
        captured["plan_cache_get"] = True
        return None, "none"

    async def _fake_plan_cache_set(_cache_key: str, payload: dict, **_kwargs):
        captured["plan_cache_set_payload"] = payload

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: _FakeRuntime())
    monkeypatch.setattr(literature_api, "_build_generative_reader_agent_tool_registry_for_paper", _fake_build_registry)
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_context", _fake_adjacent_context)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_plan_cache_get)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_set", _fake_plan_cache_set)

    payload = literature_api.ReaderGenerativePlanRequest(
        page=7,
        selected_kb_id=84,
        force_refresh=False,
        regenerate=False,
        style_intent="reader_workbench",
        user_intent="Enrich this page with public resources",
    )

    response = await literature_api.get_reader_composed_generative_plan(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert response.page == 7
    assert response.plan.shell_mode == "resource_augmented_reader"
    assert response.compose_build_mode == "compose_agent_simplified"
    assert response.compose_source_signature == "compose-sig"
    assert response.source_sig_hash == "sig-hash"
    assert response.cache_hit is True
    assert response.cache_layer == "redis"
    assert response.plan_cache_hit is False
    assert response.plan_cache_layer == "none"
    assert response.enrichment_bundle.targets[0].target_id == "p7:paragraph_1"
    assert response.scheme_choice.scheme_id == "reading_flow_stack"
    assert captured["compose_kwargs"]["paper"] is paper
    assert captured["compose_kwargs"]["page"] == 7
    assert captured["compose_kwargs"]["selected_kb_id"] == 84
    assert captured["runtime_kwargs"]["user_id"] == 5
    assert captured["runtime_kwargs"]["page"] == 7
    assert captured["runtime_kwargs"]["user_intent"] == "Enrich this page with public resources"
    assert captured["runtime_kwargs"]["tool_registry"] is not None
    assert captured["runtime_kwargs"]["allowed_tool_names"] == ["knowledge_search", "paper_read", "web_scrape", "web_search"]
    assert captured["runtime_kwargs"]["adjacent_page_context"][0]["relation"] == "previous_page"
    assert captured["runtime_kwargs"]["page_dossier"]["focus_page"] == 7
    assert captured["runtime_kwargs"]["page_dossier"]["adjacent_page_context"][1]["tables"][0]["label"] == "Table 2"
    assert captured["plan_cache_get"] is True
    assert captured["plan_cache_set_payload"]["status"] == "done"
    assert captured["adjacent_context_kwargs"]["focus_page"] == 7
    assert captured["tool_registry_kwargs"]["paper"] is paper
    assert captured["tool_registry_kwargs"]["selected_kb_id"] == 84
    assert response.adjacent_page_context[0].summary == "上一页承接摘要"
    assert response.page_dossier["focus_page"] == 7


@pytest.mark.asyncio
async def test_get_reader_composed_generative_plan_should_allow_missing_selected_kb(monkeypatch):
    paper = SimpleNamespace(id=78, title="Demo Paper", pdf_path="demo.pdf")
    captured: dict[str, object] = {}

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeComposeService:
        async def build_or_get_composed_payload(self, **kwargs):
            captured["compose_kwargs"] = kwargs
            payload = {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "compose-sig",
                "scheme_choice": {"scheme_id": "reading_flow_stack", "label": "Reading Flow"},
                "enrichment_bundle": {"version": "v1", "targets": [], "resource_modules": [], "interaction_modules": [], "meta": {}},
            }
            meta = SimpleNamespace(
                cache_hit=False,
                cache_layer="none",
                build_mode="compose_agent_simplified",
                source_signature="compose-sig",
                source_sig_hash="sig-hash",
            )
            return payload, meta

    class _FakeRuntime:
        async def build_plan(self, **kwargs):
            captured["runtime_kwargs"] = kwargs
            return {
                "version": "v1",
                "status": "done",
                "shell_mode": "resource_augmented_reader",
                "rationale": ["No KB required."],
                "resource_modules": [],
                "interaction_modules": [],
                "js_widgets": [],
                "used_tools": [],
                "tool_trace": [],
                "meta": {"page": 1},
            }

    async def _fake_build_registry(**kwargs):
        captured["tool_registry_kwargs"] = kwargs
        return None, set()

    async def _fake_cache_get(_cache_key):
        return None, "none"

    async def _fake_cache_set(_cache_key, payload, ttl_seconds=3600, **_kwargs):
        captured["cached_plan"] = payload

    async def _fake_adjacent_context(**kwargs):
        captured["adjacent_context_kwargs"] = kwargs
        return []

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: _FakeRuntime())
    monkeypatch.setattr(literature_api, "_build_generative_reader_agent_tool_registry_for_paper", _fake_build_registry)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_cache_get)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_set", _fake_cache_set)
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_context", _fake_adjacent_context)

    payload = literature_api.ReaderGenerativePlanRequest(
        page=1,
        selected_kb_id=None,
        user_intent="Build a safe default experience",
    )

    response = await literature_api.get_reader_composed_generative_plan(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert response.page == 1
    assert response.plan.status == "done"
    assert captured["compose_kwargs"]["selected_kb_id"] is None
    assert captured["tool_registry_kwargs"]["selected_kb_id"] is None
    assert captured["runtime_kwargs"]["tool_registry"] is None
    assert captured["runtime_kwargs"]["allowed_tool_names"] == []
    assert captured["runtime_kwargs"]["adjacent_page_context"] == []
    assert captured["cached_plan"]["status"] == "done"


@pytest.mark.asyncio
async def test_generative_plan_cache_get_should_backfill_redis_when_db_hits(monkeypatch):
    literature_api._generative_plan_cache_memory.clear()
    cache_key = "reader:generative:test"
    payload = {"status": "done", "meta": {"source": "db"}}
    expires_at = datetime.utcnow() + timedelta(seconds=120)
    writes: list[tuple[str, dict[str, object]]] = []

    class _FakeRedis:
        async def get(self, _cache_key):
            return None

        async def set(self, key, value, ex):
            writes.append((key, {"value": value, "ex": ex}))

    async def _fake_get_redis_client():
        return _FakeRedis()

    async def _fake_plan_cache_db_get(_cache_key, _plan_kind):
        assert _cache_key == cache_key
        return payload, expires_at

    monkeypatch.setattr(literature_api, "_get_redis_client", _fake_get_redis_client)
    monkeypatch.setattr(literature_api, "_plan_cache_db_get", _fake_plan_cache_db_get)

    result, layer = await literature_api._generative_plan_cache_get(cache_key)

    assert layer == "db"
    assert result == payload
    assert cache_key in literature_api._generative_plan_cache_memory
    assert writes and writes[0][0] == cache_key


@pytest.mark.asyncio
async def test_experience_plan_cache_set_should_persist_db_redis_and_memory(monkeypatch):
    literature_api._experience_plan_cache_memory.clear()
    cache_key = "reader:experience:test"
    payload = {"status": "done", "meta": {"source": "live"}}
    redis_writes: list[tuple[str, dict[str, object]]] = []
    db_writes: list[dict[str, object]] = []

    class _FakeRedis:
        async def set(self, key, value, ex):
            redis_writes.append((key, {"value": value, "ex": ex}))

    async def _fake_get_redis_client():
        return _FakeRedis()

    async def _fake_plan_cache_db_set(cache_key, plan_kind, payload, **kwargs):
        db_writes.append(
            {
                "cache_key": cache_key,
                "plan_kind": plan_kind,
                "payload": payload,
                **kwargs,
            }
        )

    monkeypatch.setattr(literature_api, "_get_redis_client", _fake_get_redis_client)
    monkeypatch.setattr(literature_api, "_plan_cache_db_set", _fake_plan_cache_db_set)

    await literature_api._experience_plan_cache_set(
        cache_key,
        payload,
        user_id=5,
        paper_id=78,
        page=7,
        compose_source_signature="compose-sig",
    )

    assert db_writes == [
        {
            "cache_key": cache_key,
            "plan_kind": literature_api.EXPERIENCE_PLAN_CACHE_KIND,
            "payload": payload,
            "user_id": 5,
            "paper_id": 78,
            "page": 7,
            "compose_source_signature": "compose-sig",
            "ttl_seconds": literature_api.EXPERIENCE_PLAN_CACHE_TTL_SECONDS,
        }
    ]
    assert redis_writes and redis_writes[0][0] == cache_key
    assert cache_key in literature_api._experience_plan_cache_memory


@pytest.mark.asyncio
async def test_get_reader_composed_generative_plan_should_use_plan_cache(monkeypatch):
    paper = SimpleNamespace(id=78, title="Demo Paper", pdf_path="demo.pdf")
    captured: dict[str, object] = {}

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeComposeService:
        async def build_or_get_composed_payload(self, **kwargs):
            captured["compose_kwargs"] = kwargs
            payload = {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "compose-sig",
                "scheme_choice": {"scheme_id": "reading_flow_stack", "label": "Reading Flow"},
                "enrichment_bundle": {"version": "v1", "targets": [], "resource_modules": [], "interaction_modules": [], "meta": {}},
            }
            meta = SimpleNamespace(
                cache_hit=True,
                cache_layer="db_compatible",
                build_mode="compose_agent_simplified",
                source_signature="compose-sig",
                source_sig_hash="sig-hash",
            )
            return payload, meta

    async def _fake_build_registry(**kwargs):
        captured["tool_registry_kwargs"] = kwargs
        return object(), {"paper_read", "knowledge_search"}

    async def _fake_adjacent_context(**kwargs):
        captured["adjacent_context_kwargs"] = kwargs
        return [
            {
                "page": 6,
                "relation": "previous_page",
                "reference_only": True,
                "source": "vlflash_page_ocr",
                "text": "上一页补充",
            }
        ]

    async def _fake_cache_get(_cache_key):
        return ({
            "version": "v1",
            "status": "done",
            "shell_mode": "resource_augmented_reader",
            "rationale": ["cached plan"],
            "guided_beats": [
                {
                    "beat_id": "beat_focus",
                    "title": "Focus",
                    "target_ids": ["p7:fig-1"],
                }
            ],
            "resource_modules": [],
            "interaction_modules": [],
            "js_widgets": [],
            "used_tools": ["paper_read"],
            "tool_trace": [],
            "meta": {
                "page": 7,
                "planning_brief": {"summary": "Explain the main figure."},
                "planner_output": {
                    "page_objective": "Turn the page into a guided figure explainer.",
                    "guided_beats": [{"beat_id": "beat_focus", "target_ids": ["p7:fig-1"]}],
                },
                "tool_enrichment_packet": {
                    "executed_tools": ["paper_read"],
                    "beat_packets": [{"beat_id": "beat_focus", "summary": "Cached figure context."}],
                },
                "runtime_stage_trace": [{"stage_id": "page_generation", "status": "done"}],
            },
        }, "redis")

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    monkeypatch.setattr(literature_api, "_build_generative_reader_agent_tool_registry_for_paper", _fake_build_registry)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_cache_get)

    class _ExplodingRuntime:
        async def build_plan(self, **kwargs):
            raise AssertionError("runtime should not be called when plan cache hits")

    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: _ExplodingRuntime())

    payload = literature_api.ReaderGenerativePlanRequest(
        page=7,
        selected_kb_id=84,
        user_intent="Enrich this page with public resources",
    )

    response = await literature_api.get_reader_composed_generative_plan(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert response.page == 7
    assert response.plan.status == "done"
    assert response.plan.rationale == ["cached plan"]
    assert response.plan_cache_hit is True
    assert response.plan_cache_layer == "redis"
    assert response.compose_build_mode == "compose_agent_simplified"
    assert response.compose_source_signature == "compose-sig"
    assert "adjacent_context_kwargs" not in captured
    assert "tool_registry_kwargs" not in captured


@pytest.mark.asyncio
async def test_get_reader_composed_generative_plan_should_ignore_inspect_incomplete_cache_hit(monkeypatch):
    paper = SimpleNamespace(id=78, title="Demo Paper", pdf_path="demo.pdf")
    captured: dict[str, object] = {}

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeComposeService:
        async def build_or_get_composed_payload(self, **kwargs):
            payload = {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "compose-sig",
                "scheme_choice": {"scheme_id": "reading_flow_stack", "label": "Reading Flow"},
                "enrichment_bundle": {"version": "v1", "targets": [], "resource_modules": [], "interaction_modules": [], "meta": {}},
            }
            meta = SimpleNamespace(
                cache_hit=True,
                cache_layer="db_compatible",
                build_mode="compose_agent_simplified",
                source_signature="compose-sig",
                source_sig_hash="sig-hash",
            )
            return payload, meta

    async def _fake_build_registry(**kwargs):
        captured["tool_registry_kwargs"] = kwargs
        return object(), {"paper_read"}

    async def _fake_adjacent_context(**kwargs):
        captured["adjacent_context_kwargs"] = kwargs
        return []

    async def _fake_cache_get(_cache_key):
        return ({
            "version": "v1",
            "status": "done",
            "shell_mode": "resource_augmented_reader",
            "rationale": ["thin cached plan"],
            "resource_modules": [],
            "interaction_modules": [],
            "js_widgets": [],
            "used_tools": [],
            "tool_trace": [],
            "meta": {"page": 7},
        }, "redis")

    async def _fake_cache_set(_cache_key, payload, **_kwargs):
        captured["cached_plan"] = payload

    class _Runtime:
        async def build_plan(self, **kwargs):
            captured["runtime_kwargs"] = kwargs
            return {
                "version": "v1",
                "status": "done",
                "shell_mode": "resource_augmented_reader",
                "rationale": ["live full build"],
                "guided_beats": [{"beat_id": "beat_focus", "target_ids": ["p7:fig-1"]}],
                "resource_modules": [],
                "interaction_modules": [],
                "js_widgets": [],
                "used_tools": ["paper_read"],
                "tool_trace": [],
                "meta": {
                    "planning_brief": {"summary": "Live full plan"},
                    "planner_output": {"guided_beats": [{"beat_id": "beat_focus", "target_ids": ["p7:fig-1"]}]},
                    "tool_enrichment_packet": {"beat_packets": [{"beat_id": "beat_focus", "summary": "Live packet"}]},
                    "runtime_stage_trace": [{"stage_id": "page_generation", "status": "done"}],
                },
            }

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    monkeypatch.setattr(literature_api, "_build_generative_reader_agent_tool_registry_for_paper", _fake_build_registry)
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_context", _fake_adjacent_context)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_cache_get)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_set", _fake_cache_set)
    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: _Runtime())

    payload = literature_api.ReaderGenerativePlanRequest(
        page=7,
        selected_kb_id=84,
        user_intent="Enrich this page with public resources",
    )

    response = await literature_api.get_reader_composed_generative_plan(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert response.plan_cache_hit is False
    assert response.plan_cache_layer == "none"
    assert response.plan.rationale == ["live full build"]
    assert response.plan.meta["planning_brief"]["summary"] == "Live full plan"
    assert "runtime_kwargs" in captured
    assert "tool_registry_kwargs" in captured
    assert "adjacent_context_kwargs" in captured
    assert captured["cached_plan"]["meta"]["runtime_stage_trace"]


@pytest.mark.asyncio
async def test_get_reader_experience_plan_should_build_and_cache_experience_plan(monkeypatch):
    paper = SimpleNamespace(id=78, title="Demo Paper", pdf_path="demo.pdf")
    captured: dict[str, object] = {}

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeComposeService:
        async def build_or_get_composed_payload(self, **kwargs):
            captured["compose_kwargs"] = kwargs
            payload = {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "compose-sig",
                "scheme_choice": {"scheme_id": "reading_flow_stack", "label": "Reading Flow"},
                "enrichment_bundle": {
                    "version": "v1",
                    "targets": [
                        {
                            "target_id": "p7:fig-1",
                            "node_id": "fig-1",
                            "target_kind": "figure",
                            "component_type": "FigurePanel",
                            "title": "Fig 3",
                            "excerpt": "Concordance and insight of ChatGPT on USMLE.",
                            "source_block_ids": ["p7_dm_p7_l007_b001"],
                            "source_atom_ids": [],
                            "section_label": "Results",
                            "figure_label": "Fig 3",
                            "suggested_resource_types": ["figure_explainer", "related_public_resource"],
                        }
                    ],
                    "resource_modules": [],
                    "interaction_modules": [],
                    "meta": {},
                },
            }
            meta = SimpleNamespace(
                cache_hit=True,
                cache_layer="redis",
                build_mode="compose_agent_simplified",
                source_signature="compose-sig",
                source_sig_hash="sig-hash",
            )
            return payload, meta

    class _FakeRuntime:
        async def build_plan(self, **kwargs):
            captured["plan_kwargs"] = kwargs
            return {
                "version": "v1",
                "status": "done",
                "shell_mode": "resource_augmented_reader",
                "rationale": ["Use figure-first reading."],
                "resource_modules": [],
                "interaction_modules": [],
                "js_widgets": [],
                "used_tools": ["paper_read"],
                "story_substrate": {
                    "page_id": "p7",
                    "main_claims": [{"claim_id": "claim_1", "text": "Figure 3 is key", "source_target_ids": ["p7:fig-1"]}],
                },
                "page_brief": {
                    "version": "v1",
                    "page_goal": "Explain Figure 3 first.",
                    "reader_type": "curious_generalist",
                    "primary_focus_target_id": "p7:fig-1",
                    "secondary_support_target_ids": [],
                    "reading_path": ["hero_summary", "focus_evidence", "reading_flow"],
                    "interaction_opportunities": [],
                    "resource_gaps": [],
                    "meta": {"page": 7},
                },
                "meta": {"notes": "test"},
            }

        def build_experience_plan(self, **kwargs):
            captured["experience_kwargs"] = kwargs
            return {
                "version": "v1",
                "status": "done",
                "scope": "page_focus",
                "focus_page": 7,
                "reader_profile": "curious_generalist",
                "page_story_title": "Experience title",
                "page_story_subtitle": "Experience subtitle",
                "narrative_goal": "Explain the page",
                "hero": {"title": "Hero", "subtitle": "", "summary": "Summary", "focus_label": "Fig 3", "target_ids": ["p7:fig-1"], "claim_ids": [], "meta": {}},
                "main_sections": [{"section_id": "hero", "section_type": "hero", "title": "Hero", "summary": "", "target_ids": ["p7:fig-1"], "layout_variant": "editorial_hero", "resource_module_ids": [], "interaction_module_ids": [], "widget_ids": [], "meta": {}}],
                "supporting_resources": [],
                "interactive_blocks": [],
                "widget_blocks": [],
                "reading_path": ["hero_summary", "focus_evidence", "reading_flow"],
                "used_tools": ["paper_read"],
                "meta": {"derived_from": "generative_reader_plan"},
            }

    async def _fake_build_registry(**kwargs):
        captured["tool_registry_kwargs"] = kwargs
        return object(), {"paper_read", "knowledge_search"}

    async def _fake_gen_cache_get(_cache_key):
        return None, "none"

    async def _fake_exp_cache_get(_cache_key):
        return None, "none"

    async def _fake_gen_cache_set(_cache_key, payload, ttl_seconds=3600, **_kwargs):
        captured["cached_generative"] = payload

    async def _fake_exp_cache_set(_cache_key, payload, ttl_seconds=3600, **_kwargs):
        captured["cached_experience"] = payload

    async def _fake_adjacent_context(**kwargs):
        captured["adjacent_context_kwargs"] = kwargs
        return [
            {
                "page": 6,
                "relation": "previous_page",
                "reference_only": True,
                "source": "vlflash_page_ocr",
                "summary": "上一页补充",
                "body_text": "上一页补充正文",
                "figures": [{"label": "Figure 1", "description": "上一页主图说明"}],
                "tables": [],
                "equations": [],
                "continuation_hints": ["这一页延续了上一页的图示解释。"],
            },
            {
                "page": 8,
                "relation": "next_page",
                "reference_only": True,
                "source": "vlflash_page_ocr",
                "summary": "下一页补充",
                "body_text": "下一页补充正文",
                "figures": [],
                "tables": [{"label": "Table 2", "description": "下一页表格说明"}],
                "equations": [],
                "continuation_hints": ["当前页的结论在下一页继续展开。"],
            },
        ]

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: _FakeRuntime())
    monkeypatch.setattr(literature_api, "_build_generative_reader_agent_tool_registry_for_paper", _fake_build_registry)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_gen_cache_get)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_get", _fake_exp_cache_get)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_set", _fake_gen_cache_set)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_set", _fake_exp_cache_set)
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_context", _fake_adjacent_context)

    payload = literature_api.ReaderExperiencePlanRequest(
        page=7,
        focus_page=7,
        selected_kb_id=84,
        user_intent="Create a paper experience",
        reader_profile="curious_generalist",
    )

    response = await literature_api.get_reader_experience_plan(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert response.focus_page == 7
    assert response.plan.status == "done"
    assert response.plan.page_story_title == "Experience title"
    assert response.generative_plan.status == "done"
    assert response.generative_plan_cache_hit is False
    assert response.experience_cache_hit is False
    assert response.cache_hit is True
    assert response.cache_layer == "redis"
    assert response.compose_source_signature == "compose-sig"
    assert captured["cached_generative"]["status"] == "done"
    assert captured["cached_experience"]["status"] == "done"
    assert captured["plan_kwargs"]["adjacent_page_context"][0]["page"] == 6
    assert captured["plan_kwargs"]["page_dossier"]["focus_page"] == 7
    assert captured["plan_kwargs"]["page_dossier"]["adjacent_page_context"][0]["figures"][0]["label"] == "Figure 1"
    assert captured["adjacent_context_kwargs"]["focus_page"] == 7
    assert response.adjacent_page_context[1].summary == "下一页补充"
    assert response.page_dossier["current_page"]["page"] == 7


@pytest.mark.asyncio
async def test_get_reader_experience_plan_should_persist_completed_cache_for_cached_signature_fallback(monkeypatch):
    paper = SimpleNamespace(id=78, title="Demo Paper", pdf_path="demo.pdf")
    generative_cache: dict[str, dict[str, object]] = {}
    experience_cache: dict[str, dict[str, object]] = {}

    completed_generative_plan = {
        "version": "v1",
        "status": "done",
        "shell_mode": "resource_augmented_reader",
        "guided_beats": [{"beat_id": "beat_focus", "title": "Focus", "target_ids": ["p7:fig-1"]}],
        "story_substrate": {
            "page_id": "p7",
            "main_claims": [{"claim_id": "claim_1", "text": "Figure first", "source_target_ids": ["p7:fig-1"]}],
        },
        "page_brief": {
            "page_goal": "Explain figure first.",
            "reading_path": ["hero_summary", "focus_evidence"],
            "storyboard": [{"beat_id": "beat_focus", "section_type": "focus_stage"}],
        },
        "resource_modules": [],
        "interaction_modules": [],
        "js_widgets": [],
        "meta": {"runtime_stage_trace": [{"stage_id": "plan_done", "status": "done"}]},
    }
    completed_experience_plan = {
        "version": "v1",
        "status": "done",
        "scope": "page_focus",
        "focus_page": 7,
        "reader_profile": "curious_generalist",
        "page_story_title": "Final manuscript",
        "hero": {"title": "Focus"},
        "main_sections": [{"section_id": "hero", "section_type": "hero", "title": "Hero"}],
        "supporting_resources": [],
        "interactive_blocks": [],
        "widget_blocks": [],
        "reading_path": ["hero_summary", "focus_evidence"],
        "teaching_manuscript": {
            "version": "v1",
            "status": "done",
            "segments": [
                {
                    "segment_id": "ms-body",
                    "segment_type": "body",
                    "title": "Body",
                    "teaching_text": "Final manuscript segment",
                    "target_ids": ["p7:fig-1"],
                }
            ],
        },
        "meta": {"derived_from": "generative_reader_plan"},
    }

    def _clone(value):
        return json.loads(json.dumps(value, ensure_ascii=False))

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeComposeService:
        async def build_or_get_composed_payload(self, **_kwargs):
            payload = {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "live-compose-sig",
                "cache_hit": True,
                "cache_layer": "redis",
                "enrichment_bundle": {"version": "v1", "targets": [], "resource_modules": [], "interaction_modules": [], "meta": {}},
            }
            meta = SimpleNamespace(
                cache_hit=True,
                cache_layer="redis",
                build_mode="compose_agent_simplified",
                source_signature="live-compose-sig",
                source_sig_hash="sig-hash-live",
            )
            return payload, meta

        async def get_latest_cached_payload_only(self, **_kwargs):
            return {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "cached-compose-sig",
                "cache_hit": True,
                "cache_layer": "db_latest",
                "enrichment_bundle": {"version": "v1", "targets": [], "resource_modules": [], "interaction_modules": [], "meta": {}},
            }

    class _FakeRuntime:
        async def build_plan(self, **_kwargs):
            return _clone(completed_generative_plan)

        def build_experience_plan(self, **_kwargs):
            return _clone(completed_experience_plan)

    async def _fake_build_registry(**_kwargs):
        return object(), {"paper_read"}

    async def _fake_adjacent_context(**_kwargs):
        return []

    async def _fake_gen_cache_get(cache_key):
        payload = generative_cache.get(cache_key)
        if not isinstance(payload, dict):
            return None, "none"
        return _clone(payload), "memory"

    async def _fake_gen_cache_set(cache_key, payload, ttl_seconds=3600, **_kwargs):
        _ = ttl_seconds
        generative_cache[cache_key] = _clone(payload)

    async def _fake_exp_cache_get(cache_key):
        payload = experience_cache.get(cache_key)
        if not isinstance(payload, dict):
            return None, "none"
        return _clone(payload), "memory"

    async def _fake_exp_cache_set(cache_key, payload, ttl_seconds=3600, **_kwargs):
        _ = ttl_seconds
        experience_cache[cache_key] = _clone(payload)

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: _FakeRuntime())
    monkeypatch.setattr(literature_api, "_build_generative_reader_agent_tool_registry_for_paper", _fake_build_registry)
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_context", _fake_adjacent_context)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_gen_cache_get)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_set", _fake_gen_cache_set)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_get", _fake_exp_cache_get)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_set", _fake_exp_cache_set)

    payload = literature_api.ReaderExperiencePlanRequest(
        page=7,
        focus_page=7,
        selected_kb_id=0,
        user_intent="Create a paper experience",
        reader_profile="curious_generalist",
    )

    live_response = await literature_api.get_reader_experience_plan(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )
    assert live_response.plan.page_story_title == "Final manuscript"

    plan_signature = literature_api._plan_signature(completed_generative_plan)
    primary_gen_key = literature_api._generative_plan_cache_key(
        user_id=5,
        paper_id=78,
        page=7,
        selected_kb_id=0,
        compose_source_signature="cached-compose-sig",
        user_intent="Create a paper experience",
    )
    fallback_gen_key = literature_api._generative_plan_cache_key(
        user_id=5,
        paper_id=78,
        page=7,
        selected_kb_id=0,
        compose_source_signature="",
        user_intent="Create a paper experience",
    )
    primary_exp_key = literature_api._experience_plan_cache_key(
        user_id=5,
        paper_id=78,
        focus_page=7,
        selected_kb_id=0,
        compose_source_signature="cached-compose-sig",
        generative_plan_signature=plan_signature,
        user_intent="Create a paper experience",
        reader_profile="curious_generalist",
        focus_section_ids=[],
    )
    fallback_exp_key = literature_api._experience_plan_cache_key(
        user_id=5,
        paper_id=78,
        focus_page=7,
        selected_kb_id=0,
        compose_source_signature="",
        generative_plan_signature=plan_signature,
        user_intent="Create a paper experience",
        reader_profile="curious_generalist",
        focus_section_ids=[],
    )

    assert primary_gen_key not in generative_cache
    assert fallback_gen_key in generative_cache
    assert primary_exp_key not in experience_cache
    assert fallback_exp_key in experience_cache

    cached_response = await literature_api.get_reader_experience_plan_cached(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )
    assert cached_response.plan.status == "done"
    assert cached_response.plan.page_story_title == "Final manuscript"
    assert cached_response.generative_plan_cache_hit is True
    assert cached_response.experience_cache_hit is True


@pytest.mark.asyncio
async def test_get_reader_experience_plan_should_send_adjacent_render_images_to_vl_parser(monkeypatch, tmp_path: Path):
    paper = SimpleNamespace(id=78, user_id=5, title="Demo Paper", pdf_path="demo.pdf")
    captured: dict[str, object] = {"vl_calls": []}

    pdf_path = tmp_path / "demo.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    assets_dir = tmp_path / "reader_page_assets" / "78"
    assets_dir.mkdir(parents=True, exist_ok=True)
    prev_image = assets_dir / "page_6.jpg"
    next_image = assets_dir / "page_8.jpg"
    prev_image.write_bytes(b"fake-jpg-6")
    next_image.write_bytes(b"fake-jpg-8")

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeComposeService:
        def __init__(self):
            self._reader_service = SimpleNamespace(
                _resolve_local_pdf_path=lambda **_kwargs: str(pdf_path)
            )

        async def build_or_get_composed_payload(self, **_kwargs):
            payload = {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "compose-sig",
                "ui_plan": {"components": [{"id": "paragraph_15", "type": "ParagraphProse"}]},
                "enrichment_bundle": {
                    "targets": [
                        {
                            "target_id": "p7:paragraph_15",
                            "node_id": "paragraph_15",
                            "target_kind": "paragraph",
                            "component_type": "ParagraphProse",
                        }
                    ]
                },
            }
            meta = SimpleNamespace(
                cache_hit=True,
                cache_layer="redis",
                build_mode="compose_agent_simplified",
                source_signature="compose-sig",
                source_sig_hash="sig-hash",
            )
            return payload, meta

        async def ensure_page_render_asset(self, **kwargs):
            captured.setdefault("render_asset_calls", []).append(dict(kwargs))
            return f"/api/v1/literature/reader/page-assets/{kwargs.get('paper_id')}/{kwargs.get('page')}"

        def _find_existing_page_render_asset_path(self, *, paper_id: int, page: int):
            if int(paper_id) != 78:
                return ""
            if int(page) == 6:
                return str(prev_image)
            if int(page) == 8:
                return str(next_image)
            return ""

    class _FakeRuntime:
        async def build_plan(self, **kwargs):
            captured["plan_kwargs"] = kwargs
            return {
                "version": "v1",
                "status": "done",
                "story_substrate": {"page_id": "p7"},
                "page_brief": {"page_goal": "Explain page 7", "reading_path": ["hero_summary", "focus_evidence"]},
                "resource_modules": [],
                "interaction_modules": [],
                "js_widgets": [],
                "meta": {},
            }

        def build_experience_plan(self, **kwargs):
            captured["experience_kwargs"] = kwargs
            return {
                "version": "v1",
                "status": "done",
                "scope": "page_focus",
                "focus_page": 7,
                "reader_profile": "curious_generalist",
                "page_story_title": "Paper 78 Experience",
                "page_story_subtitle": "A generated page.",
                "hero": {"title": "Focus"},
                "main_sections": [],
                "supporting_resources": [],
                "interactive_blocks": [],
                "widget_blocks": [],
                "reading_path": ["hero_summary", "focus_evidence"],
                "meta": {"derived_from": "generative_reader_plan"},
            }

    async def _fake_build_registry(**_kwargs):
        return object(), {"paper_read"}

    async def _fake_gen_cache_get(_cache_key):
        return None, "none"

    async def _fake_exp_cache_get(_cache_key):
        return None, "none"

    async def _fake_gen_cache_set(_cache_key, _payload, **_kwargs):
        return None

    async def _fake_exp_cache_set(_cache_key, _payload, **_kwargs):
        return None

    async def _fake_get_pdf_page_count(_path):
        return 8

    async def _fake_chat_json(**kwargs):
        captured["vl_calls"].append(dict(kwargs))
        relation = "previous_page"
        page = 6
        user_prompt = str(kwargs.get("user_prompt") or "")
        if "relation=next_page" in user_prompt:
            relation = "next_page"
            page = 8
        return {
            "parsed": {
                "page": page,
                "relation": relation,
                "summary": f"{relation} summary",
                "body_text": f"{relation} body",
                "figures": [],
                "tables": [],
                "equations": [],
                "continuation_hints": [f"{relation} hint"],
            },
            "raw_text": "",
        }

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: _FakeRuntime())
    monkeypatch.setattr(literature_api, "_build_generative_reader_agent_tool_registry_for_paper", _fake_build_registry)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_gen_cache_get)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_get", _fake_exp_cache_get)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_set", _fake_gen_cache_set)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_set", _fake_exp_cache_set)
    monkeypatch.setattr(literature_api, "_get_pdf_page_count", _fake_get_pdf_page_count)
    monkeypatch.setattr(literature_api.DashScopeMultimodalService, "is_available", staticmethod(lambda: True))
    monkeypatch.setattr(literature_api.DashScopeMultimodalService, "chat_json", _fake_chat_json)
    monkeypatch.setattr(literature_api.settings, "aliyun_api_key", "test-key", raising=False)
    monkeypatch.setattr(literature_api.settings, "aliyun_dashscope_api_base", "https://dashscope.test", raising=False)
    monkeypatch.setattr(literature_api.settings, "reader_mm_parser_model", "qwen3-vl-flash", raising=False)

    payload = literature_api.ReaderExperiencePlanRequest(
        page=7,
        focus_page=7,
        selected_kb_id=84,
        user_intent="Create a paper experience",
        reader_profile="curious_generalist",
    )

    response = await literature_api.get_reader_experience_plan(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    vl_calls = captured["vl_calls"]
    assert isinstance(vl_calls, list)
    assert len(vl_calls) == 2
    all_image_paths = [str(call["image_paths"][0]) for call in vl_calls]
    assert str(prev_image) in all_image_paths
    assert str(next_image) in all_image_paths
    assert all(str(call.get("model") or "") == "qwen3-vl-flash" for call in vl_calls)
    assert [item.page for item in response.adjacent_page_context] == [6, 8]
    assert response.adjacent_page_context[0].source == "vlflash_page_ocr"
    assert response.adjacent_page_context[1].source == "vlflash_page_ocr"
    assert captured["plan_kwargs"]["adjacent_page_context"][0]["page"] == 6
    assert captured["plan_kwargs"]["adjacent_page_context"][1]["page"] == 8


@pytest.mark.asyncio
async def test_get_reader_experience_plan_cached_should_derive_experience_when_generative_plan_exists(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_get_owned(_db, _user, _paper_id):
        return SimpleNamespace(id=78)

    class _FakeComposeService:
        async def get_latest_cached_payload_only(self, **kwargs):
            return {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "compose-sig",
                "cache_hit": True,
                "cache_layer": "redis",
                "components": [{"id": "paragraph_15"}],
                "enrichment_bundle": {
                    "targets": [
                        {
                            "target_id": "p7:paragraph_15",
                            "node_id": "p7:paragraph_15",
                            "target_kind": "paragraph",
                            "component_type": "ParagraphProse",
                        }
                    ]
                },
            }

    async def _fake_gen_cache_get(_cache_key):
        return {
            "version": "v1",
            "status": "done",
            "story_substrate": {"page_id": "p7"},
            "page_brief": {"page_goal": "Explain the figure", "reading_path": ["hero_summary", "focus_evidence"]},
            "resource_modules": [],
            "interaction_modules": [],
            "js_widgets": [],
            "meta": {},
        }, "redis"

    async def _fake_exp_cache_get(_cache_key):
        return None, "none"

    async def _fake_adjacent_context(**kwargs):
        captured["adjacent_context_kwargs"] = kwargs
        return [
            {
                "page": 6,
                "relation": "previous_page",
                "reference_only": True,
                "source": "vlflash_page_ocr",
                "summary": "上一页补充",
                "body_text": "上一页补充正文",
                "figures": [{"label": "Figure 1", "description": "上一页主图说明"}],
                "tables": [],
                "equations": [],
                "continuation_hints": ["这一页延续了上一页的图示解释。"],
            }
        ]

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    class _FakeRuntime:
        def build_experience_plan(self, **kwargs):
            return {
                "version": "v1",
                "status": "done",
                "scope": "page_focus",
                "focus_page": 7,
                "reader_profile": "curious_generalist",
                "page_story_title": "Paper 78 Experience",
                "page_story_subtitle": "A generated page.",
                "hero": {"title": "Focus"},
                "main_sections": [],
                "supporting_resources": [],
                "interactive_blocks": [],
                "widget_blocks": [],
                "reading_path": ["hero_summary", "focus_evidence"],
                "meta": {"derived_from": "generative_reader_plan"},
            }

    async def _fake_exp_cache_set(_cache_key, _payload, **_kwargs):
        return None

    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: _FakeRuntime())
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_gen_cache_get)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_get", _fake_exp_cache_get)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_set", _fake_exp_cache_set)
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_context", _fake_adjacent_context)

    payload = literature_api.ReaderExperiencePlanRequest(
        page=7,
        focus_page=7,
        selected_kb_id=84,
        user_intent="Create a paper experience",
        reader_profile="curious_generalist",
    )

    response = await literature_api.get_reader_experience_plan_cached(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert response.compose_status == "done"
    assert response.cache_hit is True
    assert response.cache_layer == "redis"
    assert response.generative_plan_cache_hit is True
    assert response.experience_cache_hit is True
    assert response.generative_plan.status == "done"
    assert response.plan.status == "done"
    assert response.experience_cache_layer == "derived"
    assert response.adjacent_page_context[0].summary == "上一页补充"
    assert response.page_dossier["focus_page"] == 7
    assert captured["adjacent_context_kwargs"]["focus_page"] == 7


@pytest.mark.asyncio
async def test_get_reader_experience_plan_cached_should_return_completed_cache_without_adjacent_context_build(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_get_owned(_db, _user, _paper_id):
        return SimpleNamespace(id=78)

    class _FakeComposeService:
        async def get_latest_cached_payload_only(self, **kwargs):
            return {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "compose-sig",
                "cache_hit": True,
                "cache_layer": "redis",
                "components": [{"id": "paragraph_15"}],
                "enrichment_bundle": {
                    "targets": [
                        {
                            "target_id": "p7:fig-1",
                            "node_id": "p7:fig-1",
                            "target_kind": "figure",
                            "component_type": "FigurePanel",
                        }
                    ]
                },
            }

    cached_plan = {
        "version": "v1",
        "status": "done",
        "story_substrate": {
            "page_id": "p7",
            "main_claims": [{"claim_id": "claim_1", "text": "Figure 3 is the primary result."}],
            "evidence_units": [{"evidence_id": "e1", "kind": "figure"}],
        },
        "page_brief": {
            "page_goal": "Explain the figure first.",
            "reading_path": ["hero_summary", "focus_evidence", "supporting_resources"],
            "experience_hooks": ["Figure-first walkthrough"],
            "body_flow_target_ids": ["p7:fig-1"],
            "storyboard": [
                {
                    "beat_id": "beat_focus",
                    "role": "focus_evidence",
                    "section_type": "focus_stage",
                    "title": "拆解这张图",
                    "target_ids": ["p7:fig-1"],
                }
            ],
        },
        "resource_modules": [
            {
                "module_id": "res_7_1",
                "module_type": "RelatedResourceCard",
                "target_ids": ["p7:fig-1"],
                "title": "Official context",
                "summary": "Anchor the figure in the official exam context.",
                "links": [{"label": "USMLE", "href": "https://www.usmle.org/"}],
                "source": "web",
                "interaction_mode": "stacked_cards",
                "meta": {},
            }
        ],
        "interaction_modules": [],
        "js_widgets": [],
        "meta": {
            "planning_brief": {
                "summary": "Explain the figure first.",
                "recommended_sections": ["hero_summary"],
                "tool_hints": ["paper_read"],
                "guided_beat_seed": [{"beat_id": "beat_focus"}],
            },
            "planner_output": {"guided_beats": [{"beat_id": "beat_focus"}]},
            "tool_enrichment_packet": {"resources": [{"label": "USMLE"}]},
            "runtime_stage_trace": [{"stage": "plan_complete"}],
            "adjacent_page_context": [
                {
                    "page": 6,
                    "relation": "previous_page",
                    "reference_only": True,
                    "source": "vlflash_page_ocr",
                    "summary": "",
                    "body_text": "",
                    "figures": [{"label": "Figure 2", "description": "Accuracy of Chat GPT on USMLE"}],
                    "tables": [],
                    "equations": [],
                    "continuation_hints": [
                        "The analysis continues on this page with evaluation of explanation quality.",
                        "Next section focuses on 'nonobvious insights' in AI-generated explanations.",
                    ],
                }
            ],
            "page_dossier": {
                "focus_page": 7,
                "current_page": {"page": 7, "highlights": []},
                "adjacent_page_context": [
                    {
                            "page": 6,
                            "relation": "previous_page",
                            "reference_only": True,
                            "source": "vlflash_page_ocr",
                            "summary": "",
                            "body_text": "",
                            "figures": [{"label": "Figure 2", "description": "Accuracy of Chat GPT on USMLE"}],
                            "tables": [],
                            "equations": [],
                            "continuation_hints": [
                                "The analysis continues on this page with evaluation of explanation quality.",
                                "Next section focuses on 'nonobvious insights' in AI-generated explanations.",
                            ],
                        }
                    ],
                },
            },
        }

    async def _fake_gen_cache_get(_cache_key):
        return cached_plan, "redis"

    async def _fake_exp_cache_get(_cache_key):
        return {
            "version": "v1",
            "status": "done",
            "scope": "page_focus",
            "focus_page": 7,
            "reader_profile": "curious_generalist",
            "page_story_title": "Cached experience",
            "page_story_subtitle": "Reused from final cache.",
            "hero": {"title": "Focus"},
            "teaching_manuscript": {
                "version": "v1",
                "status": "done",
                "segments": [
                    {
                        "segment_id": "ms-body",
                        "segment_type": "body",
                        "title": "顺着正文把作者的解释读完",
                        "teaching_text": "先抓住这一页在看什么。",
                        "anchor_excerpt": "",
                        "target_ids": ["p7:fig-1"],
                        "full_evidence_target_ids": ["p7:fig-1"],
                        "glossary": [],
                        "adjacent_bridge": "",
                        "reference_links": [],
                        "meta": {},
                    }
                ],
            },
            "main_sections": [
                {
                    "section_id": "supporting_resources",
                    "section_type": "supporting_resources",
                    "title": "补充背景与上下文",
                    "display_title": "补充背景与上下文",
                    "summary": "补充少量真正需要的外部背景，帮助理解正文。",
                    "display_summary": "补充少量真正需要的外部背景，帮助理解正文。",
                    "resource_module_ids": ["res_weak"],
                    "blocks": [],
                }
            ],
            "supporting_resources": [
                {
                    "module_id": "res_weak",
                    "module_type": "RelatedResourceCard",
                    "title": "延伸资源",
                    "display_title": "补充背景与上下文",
                    "summary": "补充少量真正需要的外部背景，帮助理解正文。",
                    "display_summary": "补充少量真正需要的外部背景，帮助理解正文。",
                    "links": [
                        {
                            "label": "doi.org",
                            "href": "https://doi.org/10.1371/journal.pdig.0000198.g003",
                        }
                    ],
                    "source": "fallback",
                    "meta": {},
                }
            ],
            "interactive_blocks": [],
            "widget_blocks": [],
            "reading_path": ["hero_summary", "focus_evidence"],
            "meta": {"derived_from": "generative_reader_plan"},
        }, "redis"

    async def _fake_exp_cache_set(_cache_key, payload, **_kwargs):
        captured["repaired_payload"] = payload
        return None

    async def _fake_adjacent_context(**_kwargs):
        raise AssertionError("hot cache path should not build adjacent page context")

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: GenerativeReaderAgentRuntime())
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_gen_cache_get)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_get", _fake_exp_cache_get)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_set", _fake_exp_cache_set)
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_context", _fake_adjacent_context)

    payload = literature_api.ReaderExperiencePlanRequest(
        page=7,
        focus_page=7,
        selected_kb_id=84,
        user_intent="Create a paper experience",
        reader_profile="curious_generalist",
    )

    response = await literature_api.get_reader_experience_plan_cached(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert response.cache_hit is True
    assert response.generative_plan_cache_hit is True
    assert response.generative_plan_cache_layer == "redis"
    assert response.experience_cache_hit is True
    assert response.experience_cache_layer == "redis"
    assert response.generative_plan.status == "done"
    assert response.plan.page_story_title != "Cached experience"
    assert response.plan.teaching_manuscript.segments
    body_segment = next(row for row in response.plan.teaching_manuscript.segments if row.segment_type == "body")
    assert body_segment.adjacent_bridge.startswith("读到这里时，把")
    assert "线索" in body_segment.adjacent_bridge
    assert body_segment.reference_links
    assert body_segment.reference_links[0].note
    assert response.plan.supporting_resources
    assert response.plan.supporting_resources[0].links[0]["href"] == "https://www.usmle.org/"
    assert response.adjacent_page_context[0].continuation_hints
    assert response.page_dossier["focus_page"] == 7
    assert captured["repaired_payload"]["teaching_manuscript"]["segments"]
    assert captured["repaired_payload"]["meta"]["tool_enrichment_packet"]["adjacent_page_continuity"][0]["summary"]
    assert captured["repaired_payload"]["meta"]["tool_enrichment_packet"]["adjacent_bridge_cues"][0]["text"]
    assert captured["repaired_payload"]["supporting_resources"][0]["links"][0]["href"] == "https://www.usmle.org/"


@pytest.mark.asyncio
async def test_get_reader_experience_plan_cached_should_derive_staged_generative_plan_from_cached_compose(monkeypatch):
    compose_service = LiteratureReaderComposeService()

    async def _fake_get_owned(_db, _user, _paper_id):
        return SimpleNamespace(id=78, title="Demo Paper")

    cached_compose_payload = {
        "status": "done",
        "build_mode": "compose_agent_simplified",
        "source_signature": "compose-sig",
        "cache_hit": True,
        "cache_layer": "redis",
        "components": [{"id": "paragraph_15"}],
        "enrichment_bundle": {
            "version": "v1",
            "targets": [
                {
                    "target_id": "p7:fig-1",
                    "node_id": "fig-1",
                    "target_kind": "figure",
                    "component_type": "FigurePanel",
                    "title": "Fig 3",
                    "excerpt": "Concordance and insight of ChatGPT on USMLE.",
                    "figure_label": "Fig 3",
                },
                {
                    "target_id": "p7:p-1",
                    "node_id": "p-1",
                    "target_kind": "paragraph",
                    "component_type": "ParagraphProse",
                    "title": "",
                    "excerpt": "We first examined the frequency of insight.",
                    "section_label": "Results",
                },
            ],
            "resource_modules": [],
            "interaction_modules": [],
            "meta": {},
        },
    }

    class _FakeComposeService:
        async def get_latest_cached_payload_only(self, **_kwargs):
            return dict(cached_compose_payload)

        def _ensure_payload_contract(self, *, page: int, payload: dict):
            cloned = dict(payload or {})
            cloned["generative_reader_plan"] = compose_service._build_seed_generative_reader_plan(
                page=page,
                enrichment_bundle=dict(cloned.get("enrichment_bundle") or {}),
            )
            return cloned

    async def _fake_gen_cache_get(_cache_key):
        return None, "none"

    async def _fake_exp_cache_get(_cache_key):
        raise AssertionError("experience cache should not be consulted when compose only yields provisional seed")

    async def _fake_adjacent_context(**_kwargs):
        return []

    class _ExplodingRuntime(GenerativeReaderAgentRuntime):
        def build_experience_plan(self, **_kwargs):
            raise AssertionError("provisional compose seeds must not be promoted on cached /experience")

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: _ExplodingRuntime())
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_gen_cache_get)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_get", _fake_exp_cache_get)
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_context", _fake_adjacent_context)

    payload = literature_api.ReaderExperiencePlanRequest(
        page=7,
        focus_page=7,
        selected_kb_id=84,
        user_intent="Create a paper experience",
        reader_profile="curious_generalist",
    )

    with pytest.raises(HTTPException) as exc:
        await literature_api.get_reader_experience_plan_cached(
            paper_id=78,
            payload=payload,
            db=SimpleNamespace(),
            current_user=SimpleNamespace(id=5),
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "No completed experience manuscript cached for this page"


@pytest.mark.asyncio
async def test_get_reader_experience_plan_cached_should_keep_compose_only_done_plan_provisional_without_runtime_plan(monkeypatch):
    compose_service = LiteratureReaderComposeService()

    async def _fake_get_owned(_db, _user, _paper_id):
        return SimpleNamespace(id=78, title="Demo Paper")

    class _FakeComposeService:
        async def get_latest_cached_payload_only(self, **_kwargs):
            seed_plan = compose_service._build_seed_generative_reader_plan(
                page=7,
                enrichment_bundle={
                    "version": "v1",
                    "targets": [
                        {
                            "target_id": "p7:fig-1",
                            "node_id": "fig-1",
                            "target_kind": "figure",
                            "component_type": "FigurePanel",
                            "title": "Fig 3",
                            "excerpt": "Concordance and insight of ChatGPT on USMLE.",
                            "figure_label": "Fig 3",
                        }
                    ],
                    "resource_modules": [],
                    "interaction_modules": [],
                    "meta": {},
                },
            )
            seed_plan["status"] = "done"
            return {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "compose-sig",
                "cache_hit": True,
                "cache_layer": "redis",
                "generative_reader_plan": seed_plan,
                "enrichment_bundle": {
                    "version": "v1",
                    "targets": [
                        {
                            "target_id": "p7:fig-1",
                            "node_id": "fig-1",
                            "target_kind": "figure",
                            "component_type": "FigurePanel",
                            "title": "Fig 3",
                            "excerpt": "Concordance and insight of ChatGPT on USMLE.",
                            "figure_label": "Fig 3",
                        }
                    ],
                    "resource_modules": [],
                    "interaction_modules": [],
                    "meta": {},
                },
            }

    async def _fake_gen_cache_get(_cache_key):
        return None, "none"

    async def _fake_exp_cache_get(_cache_key):
        raise AssertionError("experience cache should not be consulted for provisional compose-only plans")

    async def _fake_adjacent_context(**_kwargs):
        return []

    class _ExplodingRuntime(GenerativeReaderAgentRuntime):
        def build_experience_plan(self, **_kwargs):
            raise AssertionError("compose-only provisional plans must not be promoted on cached /experience")

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: _ExplodingRuntime())
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_gen_cache_get)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_get", _fake_exp_cache_get)
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_context", _fake_adjacent_context)

    payload = literature_api.ReaderExperiencePlanRequest(
        page=7,
        focus_page=7,
        selected_kb_id=84,
        user_intent="Create a paper experience",
        reader_profile="curious_generalist",
    )

    with pytest.raises(HTTPException) as exc:
        await literature_api.get_reader_experience_plan_cached(
            paper_id=78,
            payload=payload,
            db=SimpleNamespace(),
            current_user=SimpleNamespace(id=5),
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "No completed experience manuscript cached for this page"


@pytest.mark.asyncio
async def test_get_reader_experience_plan_cached_should_prefer_compose_derived_plan_over_scaffold_cache(monkeypatch):
    runtime = GenerativeReaderAgentRuntime()
    compose_service = LiteratureReaderComposeService()
    captured: dict[str, object] = {"experience_cache_keys": []}

    async def _fake_get_owned(_db, _user, _paper_id):
        return SimpleNamespace(id=78, title="Demo Paper")

    cached_compose_payload = {
        "status": "done",
        "build_mode": "compose_agent_simplified",
        "source_signature": "compose-sig",
        "cache_hit": True,
        "cache_layer": "redis",
        "components": [{"id": "paragraph_15"}],
        "enrichment_bundle": {
            "version": "v1",
            "targets": [
                {
                    "target_id": "p7:fig-1",
                    "node_id": "fig-1",
                    "target_kind": "figure",
                    "component_type": "FigurePanel",
                    "title": "Fig 3",
                    "excerpt": "Concordance and insight of ChatGPT on USMLE.",
                    "figure_label": "Fig 3",
                },
                {
                    "target_id": "p7:p-1",
                    "node_id": "p-1",
                    "target_kind": "paragraph",
                    "component_type": "ParagraphProse",
                    "title": "",
                    "excerpt": "We first examined the frequency of insight.",
                    "section_label": "Results",
                },
            ],
            "resource_modules": [],
            "interaction_modules": [],
            "meta": {},
        },
    }

    scaffold_cached_plan = {
        "version": "v1",
        "status": "done",
        "shell_mode": "resource_augmented_reader",
        "story_substrate": {"page_id": "p7"},
        "page_brief": {"page_goal": "Explain the page", "reading_path": ["hero_summary"]},
        "resource_modules": [],
        "interaction_modules": [],
        "js_widgets": [],
        "meta": {},
    }

    class _FakeComposeService:
        async def get_latest_cached_payload_only(self, **_kwargs):
            return dict(cached_compose_payload)

        def _ensure_payload_contract(self, *, page: int, payload: dict):
            cloned = dict(payload or {})
            cloned["generative_reader_plan"] = compose_service._build_seed_generative_reader_plan(
                page=page,
                enrichment_bundle=dict(cloned.get("enrichment_bundle") or {}),
            )
            return cloned

    async def _fake_gen_cache_get(_cache_key):
        return dict(scaffold_cached_plan), "redis"

    payload = literature_api.ReaderExperiencePlanRequest(
        page=7,
        focus_page=7,
        selected_kb_id=84,
        user_intent="Create a paper experience",
        reader_profile="curious_generalist",
    )
    old_plan_signature = literature_api._plan_signature(scaffold_cached_plan)
    stale_experience_cache_key = literature_api._experience_plan_cache_key(
        user_id=5,
        paper_id=78,
        focus_page=7,
        selected_kb_id=84,
        compose_source_signature="compose-sig",
        generative_plan_signature=old_plan_signature,
        user_intent=str(payload.user_intent or "").strip(),
        reader_profile=str(payload.reader_profile or "").strip(),
        focus_section_ids=list(payload.focus_section_ids or []),
    )
    stale_cached_experience = {
        "version": "v1",
        "status": "done",
        "scope": "page_focus",
        "focus_page": 7,
        "reader_profile": "curious_generalist",
        "page_story_title": "Scaffold experience",
        "page_story_subtitle": "Thin cached output",
        "hero": {"title": "Thin"},
        "main_sections": [],
        "guided_beats": [],
        "supporting_resources": [],
        "interactive_blocks": [],
        "widget_blocks": [],
        "reading_path": ["hero_summary"],
        "meta": {"derived_from": "stale_scaffold_cache"},
    }

    async def _fake_exp_cache_get(cache_key):
        captured["experience_cache_keys"].append(cache_key)
        if cache_key == stale_experience_cache_key:
            return stale_cached_experience, "redis"
        return None, "none"

    async def _fake_exp_cache_set(cache_key, payload, ttl_seconds=3600, **_kwargs):
        captured["cached_experience_key"] = cache_key
        captured["cached_experience"] = payload

    async def _fake_adjacent_context(**_kwargs):
        return []

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: runtime)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_gen_cache_get)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_get", _fake_exp_cache_get)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_set", _fake_exp_cache_set)
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_context", _fake_adjacent_context)

    response = await literature_api.get_reader_experience_plan_cached(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert response.generative_plan_cache_hit is False
    assert response.generative_plan_cache_layer == "derived"
    assert response.generative_plan.meta["derived_from"] == "cached_compose_payload"
    assert response.generative_plan.page_brief.storyboard
    assert response.generative_plan.meta["runtime_stage_trace"]
    assert response.plan.page_story_title != "Scaffold experience"
    assert response.plan.guided_beats
    assert response.experience_cache_hit is True
    assert response.experience_cache_layer == "derived"
    assert captured["experience_cache_keys"] == [captured["cached_experience_key"]]
    assert captured["experience_cache_keys"][0] != stale_experience_cache_key


def test_reader_plan_cache_keys_should_use_v33_contract_namespace():
    gen_key = literature_api._generative_plan_cache_key(
        user_id=5,
        paper_id=78,
        page=7,
        selected_kb_id=84,
        compose_source_signature="compose-sig",
        user_intent="Create a paper experience",
    )
    exp_key = literature_api._experience_plan_cache_key(
        user_id=5,
        paper_id=78,
        focus_page=7,
        selected_kb_id=84,
        compose_source_signature="compose-sig",
        generative_plan_signature="plan-sig",
        user_intent="Create a paper experience",
        reader_profile="curious_generalist",
        focus_section_ids=[],
    )

    assert gen_key.startswith("lit:genplan:v33:")
    assert exp_key.startswith("lit:experience:v33:")


@pytest.mark.asyncio
async def test_get_reader_experience_plan_cached_should_prefer_completed_manuscript_artifacts(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_get_owned(_db, _user, _paper_id):
        return SimpleNamespace(id=78)

    class _FakeComposeService:
        async def get_latest_cached_payload_only(self, **_kwargs):
            return {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "compose-sig",
                "cache_hit": True,
                "cache_layer": "redis",
                "enrichment_bundle": {
                    "version": "v1",
                    "targets": [{"target_id": "p7:fig-1", "target_kind": "figure"}],
                    "resource_modules": [],
                    "interaction_modules": [],
                    "meta": {},
                },
            }

    async def _fake_gen_cache_get(_cache_key):
        return {
            "version": "v1",
            "status": "done",
            "shell_mode": "resource_augmented_reader",
            "guided_beats": [{"beat_id": "beat_focus", "title": "Focus", "target_ids": ["p7:fig-1"]}],
            "story_substrate": {
                "page_id": "p7",
                "main_claims": [{"claim_id": "claim_1", "text": "Figure first", "source_target_ids": ["p7:fig-1"]}],
            },
            "page_brief": {
                "page_goal": "Explain figure 3 first.",
                "reading_path": ["hero_summary", "focus_evidence"],
                "storyboard": [{"beat_id": "beat_focus", "section_type": "focus_stage"}],
            },
            "resource_modules": [
                {
                    "module_id": "res_7_1",
                    "module_type": "RelatedResourceCard",
                    "target_ids": ["p7:fig-1"],
                    "title": "USMLE context",
                    "summary": "Context",
                    "links": [{"label": "USMLE", "href": "https://www.usmle.org/"}],
                    "source": "web",
                    "interaction_mode": "stacked_cards",
                    "meta": {},
                }
            ],
            "interaction_modules": [],
            "js_widgets": [],
            "meta": {"runtime_stage_trace": [{"stage_id": "plan_done", "status": "done"}]},
        }, "redis"

    async def _fake_exp_cache_get(_cache_key):
        return {
            "version": "v1",
            "status": "done",
            "scope": "page_focus",
            "focus_page": 7,
            "reader_profile": "curious_generalist",
            "page_story_title": "Intermediate manuscript",
            "hero": {"title": "Draft"},
            "main_sections": [],
            "supporting_resources": [],
            "interactive_blocks": [],
            "widget_blocks": [],
            "reading_path": ["hero_summary"],
            "teaching_manuscript": {"version": "v1", "status": "draft", "segments": []},
            "meta": {"seed_plan": True},
        }, "redis"

    async def _fake_exp_cache_set(_cache_key, payload, **_kwargs):
        captured["cached_experience"] = payload

    class _FakeRuntime:
        def build_experience_plan(self, **_kwargs):
            return {
                "version": "v1",
                "status": "done",
                "scope": "page_focus",
                "focus_page": 7,
                "reader_profile": "curious_generalist",
                "page_story_title": "Final manuscript",
                "page_story_subtitle": "Final-only experience artifact",
                "hero": {"title": "Focus"},
                "main_sections": [{"section_id": "hero", "section_type": "hero", "title": "Hero"}],
                "supporting_resources": [],
                "interactive_blocks": [],
                "widget_blocks": [],
                "reading_path": ["hero_summary", "focus_evidence"],
                "teaching_manuscript": {
                    "version": "v1",
                    "status": "done",
                    "segments": [
                        {
                            "segment_id": "ms-body",
                            "segment_type": "body",
                            "title": "Body",
                            "teaching_text": "Final manuscript segment",
                            "target_ids": ["p7:fig-1"],
                        }
                    ],
                },
                "meta": {"derived_from": "generative_reader_plan"},
            }

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: _FakeRuntime())
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_gen_cache_get)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_get", _fake_exp_cache_get)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_set", _fake_exp_cache_set)

    response = await literature_api.get_reader_experience_plan_cached(
        paper_id=78,
        payload=literature_api.ReaderExperiencePlanRequest(page=7, focus_page=7, selected_kb_id=84),
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert response.plan.status == "done"
    assert response.plan.page_story_title == "Final manuscript"
    assert response.plan.teaching_manuscript
    assert response.plan.teaching_manuscript.status == "done"
    assert response.plan.hero.display_title == "Focus"
    assert response.compose_payload == {}
    assert response.page_dossier == {}
    assert response.adjacent_page_context == []
    assert response.experience_cache_hit is True
    assert response.experience_cache_layer == "derived"
    assert captured["cached_experience"]["teaching_manuscript"]["status"] == "done"


@pytest.mark.asyncio
async def test_get_reader_experience_plan_cached_should_keep_intermediate_artifacts_off_experience(monkeypatch):
    async def _fake_get_owned(_db, _user, _paper_id):
        return SimpleNamespace(id=78)

    class _FakeComposeService:
        async def get_latest_cached_payload_only(self, **_kwargs):
            return {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "compose-sig",
                "cache_hit": True,
                "cache_layer": "redis",
                "enrichment_bundle": {"version": "v1", "targets": [], "resource_modules": [], "interaction_modules": [], "meta": {}},
            }

    async def _fake_gen_cache_get(_cache_key):
        return {
            "version": "v1",
            "status": "draft",
            "shell_mode": "resource_augmented_reader",
            "resource_modules": [],
            "interaction_modules": [],
            "js_widgets": [],
            "meta": {"seed_plan": True},
        }, "redis"

    async def _fake_exp_cache_get(_cache_key):
        raise AssertionError("experience cache should not be consulted when generative artifact is intermediate")

    class _FakeRuntime:
        def build_experience_plan(self, **_kwargs):
            raise AssertionError("intermediate artifacts must not be promoted to /experience")

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: _FakeRuntime())
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_gen_cache_get)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_get", _fake_exp_cache_get)

    with pytest.raises(HTTPException) as exc:
        await literature_api.get_reader_experience_plan_cached(
            paper_id=78,
            payload=literature_api.ReaderExperiencePlanRequest(page=7, focus_page=7, selected_kb_id=84),
            db=SimpleNamespace(),
            current_user=SimpleNamespace(id=5),
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "No completed experience manuscript cached for this page"


@pytest.mark.asyncio
async def test_get_reader_composed_generative_plan_should_keep_workbench_staging_and_provenance_visible(monkeypatch):
    paper = SimpleNamespace(id=78, title="Demo Paper", pdf_path="demo.pdf")

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeComposeService:
        async def build_or_get_composed_payload(self, **_kwargs):
            payload = {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "compose-sig",
                "scheme_choice": {"scheme_id": "reading_flow_stack", "label": "Reading Flow"},
                "enrichment_bundle": {"version": "v1", "targets": [], "resource_modules": [], "interaction_modules": [], "meta": {}},
            }
            meta = SimpleNamespace(
                cache_hit=True,
                cache_layer="redis",
                build_mode="compose_agent_simplified",
                source_signature="compose-sig",
                source_sig_hash="sig-hash",
            )
            return payload, meta

    async def _fake_cache_get(_cache_key):
        return ({
            "version": "v1",
            "status": "done",
            "shell_mode": "resource_augmented_reader",
            "story_substrate": {"page_id": "p7", "main_claims": [{"claim_id": "c1", "text": "Core claim"}]},
            "page_brief": {"page_goal": "Explain the page", "reading_path": ["hero_summary", "focus_evidence"]},
            "guided_beats": [{"beat_id": "beat_focus", "title": "Focus"}],
            "resource_modules": [],
            "interaction_modules": [],
            "js_widgets": [],
            "tool_trace": [{"tool_name": "web_search", "status": "done"}],
            "meta": {
                "planning_brief": {"summary": "Prefer figure-first walkthrough."},
                "planner_output": {"guided_beats": [{"beat_id": "beat_focus"}]},
                "tool_enrichment_packet": {"executed_tools": ["paper_read", "web_search"]},
                "runtime_stage_trace": [{"stage_id": "critic_review", "status": "done"}],
                "contract_validation": {"status": "validated", "contract": "generative_plan_v2"},
                "adjacent_page_context": [{"page": 6, "relation": "previous_page", "reference_only": True, "source": "vlflash_page_ocr"}],
                "page_dossier": {"focus_page": 7, "current_page": {"page": 7}, "adjacent_page_context": [{"page": 6}]},
            },
        }, "redis")

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_cache_get)

    class _ExplodingRuntime:
        async def build_plan(self, **_kwargs):
            raise AssertionError("runtime should not run when workbench cache inspect payload is complete")

    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: _ExplodingRuntime())

    response = await literature_api.get_reader_composed_generative_plan(
        paper_id=78,
        payload=literature_api.ReaderGenerativePlanRequest(
            page=7,
            selected_kb_id=84,
            style_intent="reader_workbench",
            user_intent="Inspect staging and provenance",
        ),
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert response.plan_cache_hit is True
    assert response.plan_cache_layer == "redis"
    assert response.plan.story_substrate.page_id == "p7"
    assert response.plan.page_brief.page_goal == "Explain the page"
    assert response.plan.tool_trace[0]["tool_name"] == "web_search"
    assert response.page_dossier["focus_page"] == 7
    assert response.adjacent_page_context[0].page == 6
    assert response.plan.meta["contract_validation"]["contract"] == "generative_plan_v2"
    assert response.plan.meta["runtime_stage_trace"][0]["stage_id"] == "critic_review"
    assert response.plan.meta["tool_enrichment_packet"]["executed_tools"] == ["paper_read", "web_search"]


@pytest.mark.asyncio
async def test_get_reader_experience_plan_cached_should_repair_missing_cached_generative_ids(monkeypatch):
    runtime = GenerativeReaderAgentRuntime()

    async def _fake_get_owned(_db, _user, _paper_id):
        return SimpleNamespace(id=78, title="Demo Paper")

    class _FakeComposeService:
        async def get_latest_cached_payload_only(self, **kwargs):
            return {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "compose-sig",
                "cache_hit": True,
                "cache_layer": "redis",
                "components": [{"id": "paragraph_15"}],
                "enrichment_bundle": {
                    "version": "v1",
                    "targets": [
                        {
                            "target_id": "p7:fig-1",
                            "node_id": "fig-1",
                            "target_kind": "figure",
                            "component_type": "FigurePanel",
                            "title": "Fig 3",
                            "excerpt": "Concordance and insight of ChatGPT on USMLE.",
                            "figure_label": "Fig 3",
                        },
                        {
                            "target_id": "p7:p-1",
                            "node_id": "p-1",
                            "target_kind": "paragraph",
                            "component_type": "ParagraphProse",
                            "title": "",
                            "excerpt": "We first examined the frequency of insight.",
                            "section_label": "Results",
                        },
                    ],
                    "resource_modules": [],
                    "interaction_modules": [],
                    "meta": {},
                },
            }

    cached_plan = {
        "version": "v1",
        "status": "done",
        "shell_mode": "resource_augmented_reader",
        "story_substrate": {
            "version": "v1",
            "page_id": "p7",
            "main_claims": [
                {"claim_id": "claim_1", "text": "Figure 3 carries the primary result.", "source_target_ids": ["p7:fig-1"]},
            ],
            "evidence_units": [
                {"evidence_id": "e1", "kind": "figure", "role": "primary_visual_evidence", "source_target_ids": ["p7:fig-1"]},
            ],
            "terms_to_explain": [
                {"term": "Concordance", "reason": "metric", "source_target_ids": ["p7:p-1"]},
            ],
            "background_gaps": [
                {"topic": "USMLE context", "reason": "reader context", "suggested_resource_type": "official_context_links"},
            ],
            "narrative_turns": [
                {"turn_id": "t1", "kind": "key_finding", "label": "Result", "target_ids": ["p7:p-1"]},
            ],
            "meta": {},
        },
        "page_brief": {
            "version": "v1",
            "page_goal": "Explain the figure first, then connect supporting resources, then unpack terms.",
            "reader_type": "curious_generalist",
            "page_archetype": "figure_explainer",
            "hero_angle": "Use the figure as the anchor for the page.",
            "primary_focus_target_id": "p7:fig-1",
            "secondary_support_target_ids": ["p7:p-1"],
            "body_flow_target_ids": ["p7:fig-1", "p7:p-1"],
            "reading_path": ["hero_summary", "focus_evidence", "reading_flow", "context_explainer", "supporting_resources", "explore_questions"],
            "interaction_opportunities": ["expand_figure_panels", "open_supporting_resources"],
            "resource_gaps": ["USMLE context"],
            "experience_hooks": ["Figure-first guided tour"],
            "resource_strategy": "Bring in official context before explanatory modules.",
            "storyboard": [
                {"beat_id": "beat_focus", "role": "focus_evidence", "section_type": "focus_stage", "title": "拆解这张图", "target_ids": ["p7:fig-1"], "tool_objectives": ["figure_context"], "priority": 2},
                {"beat_id": "beat_explain", "role": "clarify_terms", "section_type": "explainer_cluster", "title": "读懂关键术语", "target_ids": ["p7:p-1"], "tool_objectives": ["term_explain"], "priority": 4},
                {"beat_id": "beat_context", "role": "add_context", "section_type": "supporting_resources", "title": "补充背景与上下文", "target_ids": ["p7:p-1"], "tool_objectives": ["why_it_matters"], "priority": 5},
            ],
            "content_budget": {"max_claim_cards": 2, "max_hooks": 2, "max_resource_modules": 2, "max_explainer_modules": 2, "max_question_modules": 1, "max_widgets": 1},
            "meta": {"include_story_map": False},
        },
        "rationale": ["Anchor the page on the figure, then move outward."],
        "resource_modules": [
            {
                "module_type": "RelatedResourceCard",
                "target_ids": ["p7:p-1"],
                "title": "Official USMLE context",
                "summary": "Ground the figure in exam structure.",
                "links": [{"label": "USMLE", "href": "https://www.usmle.org/"}],
                "source": "web",
                "interaction_mode": "stacked_cards",
                "meta": {},
            }
        ],
        "interaction_modules": [
            {
                "module_type": "GlossaryPanel",
                "target_ids": ["p7:p-1"],
                "title": "Key terms",
                "props": {"terms": [{"term": "Concordance", "definition": "Agreement metric."}]},
                "source": "agent",
                "meta": {},
            }
        ],
        "js_widgets": [
            {
                "widget_type": "figure-focus-accordion",
                "target_ids": ["p7:fig-1"],
                "title": "Figure walk-through",
                "data_requirements": ["figure_explainer"],
                "props": {"panels": [{"label": "Panel A", "summary": "Primary view."}]},
                "meta": {},
            }
        ],
        "used_tools": ["paper_read", "web_search"],
        "tool_trace": [],
        "meta": {},
    }

    async def _fake_gen_cache_get(_cache_key):
        return cached_plan, "redis"

    async def _fake_exp_cache_get(_cache_key):
        return None, "none"

    async def _fake_exp_cache_set(_cache_key, _payload, **_kwargs):
        return None

    async def _fake_adjacent_context(**_kwargs):
        return []

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: runtime)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_gen_cache_get)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_get", _fake_exp_cache_get)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_set", _fake_exp_cache_set)
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_context", _fake_adjacent_context)

    payload = literature_api.ReaderExperiencePlanRequest(
        page=7,
        focus_page=7,
        selected_kb_id=84,
        user_intent="Create a paper experience",
        reader_profile="curious_generalist",
    )

    response = await literature_api.get_reader_experience_plan_cached(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    resource_id = response.generative_plan.resource_modules[0].module_id
    interaction_id = response.generative_plan.interaction_modules[0].module_id
    widget_id = response.generative_plan.js_widgets[0].widget_id

    assert resource_id.startswith("res_7_")
    assert interaction_id.startswith("int_7_")
    assert widget_id.startswith("widget_7_")
    assert response.generative_plan.meta["id_materialization"]["status"] == "repaired_missing_ids"
    assert response.plan.status == "done"
    assert response.plan.supporting_resources[0].module_id == resource_id
    assert response.plan.interactive_blocks[0].module_id == interaction_id
    assert response.plan.widget_blocks[0].widget_id == widget_id


@pytest.mark.asyncio
async def test_get_reader_experience_plan_cached_should_repair_sparse_compose_seed_plan(monkeypatch):
    async def _fake_get_owned(_db, _user, _paper_id):
        return SimpleNamespace(id=78, title="Demo Paper")

    class _FakeComposeService:
        async def get_latest_cached_payload_only(self, **kwargs):
            return {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "compose-sig",
                "cache_hit": True,
                "cache_layer": "redis",
                "generative_reader_plan": {
                    "version": "v1",
                    "status": "draft",
                    "shell_mode": "resource_augmented_reader",
                    "rationale": ["cached seed"],
                    "resource_modules": [],
                    "interaction_modules": [],
                    "js_widgets": [],
                    "meta": {"source": "compose_cache"},
                },
                "enrichment_bundle": {
                    "version": "v1",
                    "targets": [],
                    "resource_modules": [],
                    "interaction_modules": [],
                    "meta": {},
                },
            }

    async def _fake_gen_cache_get(_cache_key):
        return None, "none"

    async def _fake_exp_cache_get(_cache_key):
        raise AssertionError("experience cache should not be consulted for repaired provisional compose seeds")

    async def _fake_adjacent_context(**_kwargs):
        return []

    class _ExplodingRuntime(GenerativeReaderAgentRuntime):
        def build_experience_plan(self, **_kwargs):
            raise AssertionError("repaired provisional compose seeds must not be promoted on cached /experience")

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: _ExplodingRuntime())
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_gen_cache_get)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_get", _fake_exp_cache_get)
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_context", _fake_adjacent_context)

    payload = literature_api.ReaderExperiencePlanRequest(
        page=7,
        focus_page=7,
        selected_kb_id=84,
        user_intent="Create a paper experience",
        reader_profile="curious_generalist",
    )

    with pytest.raises(HTTPException) as exc:
        await literature_api.get_reader_experience_plan_cached(
            paper_id=78,
            payload=payload,
            db=SimpleNamespace(),
            current_user=SimpleNamespace(id=5),
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "No completed experience manuscript cached for this page"


@pytest.mark.asyncio
async def test_get_reader_experience_plan_should_allow_missing_selected_kb(monkeypatch):
    paper = SimpleNamespace(id=78, title="Demo Paper", pdf_path="demo.pdf")
    captured: dict[str, object] = {}

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeComposeService:
        async def build_or_get_composed_payload(self, **kwargs):
            captured["compose_kwargs"] = kwargs
            payload = {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "compose-sig",
                "scheme_choice": {"scheme_id": "reading_flow_stack", "label": "Reading Flow"},
                "enrichment_bundle": {"version": "v1", "targets": [], "resource_modules": [], "interaction_modules": [], "meta": {}},
            }
            meta = SimpleNamespace(
                cache_hit=False,
                cache_layer="none",
                build_mode="compose_agent_simplified",
                source_signature="compose-sig",
                source_sig_hash="sig-hash",
            )
            return payload, meta

    class _FakeRuntime:
        async def build_plan(self, **kwargs):
            captured["plan_kwargs"] = kwargs
            return {
                "version": "v1",
                "status": "draft",
                "shell_mode": "resource_augmented_reader",
                "resource_modules": [],
                "interaction_modules": [],
                "js_widgets": [],
                "meta": {},
            }

        def build_experience_plan(self, **kwargs):
            captured["experience_kwargs"] = kwargs
            return {
                "version": "v1",
                "status": "done",
                "scope": "page_focus",
                "focus_page": 1,
                "reader_profile": "curious_generalist",
                "page_story_title": "Fallback experience",
                "page_story_subtitle": "No KB selected.",
                "hero": {"title": "Focus"},
                "main_sections": [],
                "supporting_resources": [],
                "interactive_blocks": [],
                "widget_blocks": [],
                "reading_path": ["hero_summary"],
                "meta": {"derived_from": "generative_reader_plan"},
            }

    async def _fake_build_registry(**kwargs):
        captured["tool_registry_kwargs"] = kwargs
        return None, set()

    async def _fake_adjacent_context(**kwargs):
        captured["adjacent_context_kwargs"] = kwargs
        return []

    async def _fake_gen_cache_get(_cache_key):
        return None, "none"

    async def _fake_exp_cache_get(_cache_key):
        return None, "none"

    async def _fake_gen_cache_set(_cache_key, payload, ttl_seconds=3600, **_kwargs):
        captured["cached_generative"] = payload

    async def _fake_exp_cache_set(_cache_key, payload, ttl_seconds=3600, **_kwargs):
        captured["cached_experience"] = payload

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: _FakeRuntime())
    monkeypatch.setattr(literature_api, "_build_generative_reader_agent_tool_registry_for_paper", _fake_build_registry)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_gen_cache_get)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_get", _fake_exp_cache_get)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_set", _fake_gen_cache_set)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_set", _fake_exp_cache_set)
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_context", _fake_adjacent_context)

    payload = literature_api.ReaderExperiencePlanRequest(
        page=1,
        focus_page=1,
        selected_kb_id=None,
        user_intent="Create a safe default experience",
        reader_profile="curious_generalist",
    )

    response = await literature_api.get_reader_experience_plan(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert response.focus_page == 1
    assert response.plan.status == "done"
    assert captured["compose_kwargs"]["selected_kb_id"] is None
    assert captured["tool_registry_kwargs"]["selected_kb_id"] is None
    assert captured["plan_kwargs"]["tool_registry"] is None
    assert captured["plan_kwargs"]["allowed_tool_names"] == []
    assert captured["plan_kwargs"]["adjacent_page_context"] == []
    assert captured["cached_generative"]["status"] == "draft"
    assert captured["cached_experience"]["status"] == "done"


@pytest.mark.asyncio
async def test_reader_experience_flow_should_progress_from_seed_to_full_plan_then_cache_hit(monkeypatch):
    paper = SimpleNamespace(id=78, title="Demo Paper", pdf_path="demo.pdf")
    captured = {"build_plan_calls": 0, "build_experience_calls": 0}
    generative_cache: dict[str, dict[str, object]] = {}
    experience_cache: dict[str, dict[str, object]] = {}

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeComposeService:
        async def get_latest_cached_payload_only(self, **_kwargs):
            return {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "compose-sig",
                "cache_hit": True,
                "cache_layer": "redis",
                "components": [{"id": "paragraph_15"}],
                "enrichment_bundle": {"targets": []},
            }

        async def build_or_get_composed_payload(self, **_kwargs):
            payload = await self.get_latest_cached_payload_only()
            meta = SimpleNamespace(
                cache_hit=True,
                cache_layer="redis",
                build_mode="compose_agent_simplified",
                source_signature="compose-sig",
                source_sig_hash="sig-hash",
            )
            return payload, meta

    class _FakeRuntime:
        def build_seed_plan(self, **kwargs):
            return {
                "version": "v1",
                "status": "draft",
                "shell_mode": "resource_augmented_reader",
                "story_substrate": {"page_id": f"p{kwargs['page']}"},
                "page_brief": {"page_goal": "Seed plan", "reading_path": ["hero_summary"]},
                "resource_modules": [],
                "interaction_modules": [],
                "js_widgets": [],
                "meta": {"seed_plan": True},
            }

        async def build_plan(self, **kwargs):
            captured["build_plan_calls"] += 1
            return {
                "version": "v1",
                "status": "done",
                "shell_mode": "resource_augmented_reader",
                "story_substrate": {"page_id": f"p{kwargs['page']}"},
                "page_brief": {"page_goal": "Full plan", "reading_path": ["hero_summary", "focus_evidence"]},
                "resource_modules": [],
                "interaction_modules": [],
                "js_widgets": [],
                "used_tools": ["paper_read"],
                "meta": {"full_plan": True},
            }

        def build_experience_plan(self, **kwargs):
            captured["build_experience_calls"] += 1
            generative_plan = kwargs["generative_plan"]
            plan_status = str(generative_plan.get("status") or "").strip()
            return {
                "version": "v1",
                "status": "done",
                "scope": "page_focus",
                "focus_page": kwargs["focus_page"],
                "reader_profile": kwargs["reader_profile"],
                "page_story_title": "Seed experience" if plan_status == "draft" else "Full experience",
                "page_story_subtitle": "Derived from plan",
                "hero": {"title": "Focus"},
                "main_sections": [],
                "supporting_resources": [],
                "interactive_blocks": [],
                "widget_blocks": [],
                "reading_path": list(generative_plan.get("page_brief", {}).get("reading_path") or []),
                "meta": {"derived_from": "generative_reader_plan"},
            }

    async def _fake_build_registry(**_kwargs):
        return object(), {"paper_read"}

    async def _fake_gen_cache_get(cache_key):
        return generative_cache.get(cache_key), ("redis" if cache_key in generative_cache else "none")

    async def _fake_exp_cache_get(cache_key):
        return experience_cache.get(cache_key), ("redis" if cache_key in experience_cache else "none")

    async def _fake_gen_cache_set(cache_key, payload, ttl_seconds=3600, **_kwargs):
        generative_cache[cache_key] = payload

    async def _fake_exp_cache_set(cache_key, payload, ttl_seconds=3600, **_kwargs):
        experience_cache[cache_key] = payload

    async def _fake_adjacent_context(**_kwargs):
        return []

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: _FakeRuntime())
    monkeypatch.setattr(literature_api, "_build_generative_reader_agent_tool_registry_for_paper", _fake_build_registry)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_gen_cache_get)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_get", _fake_exp_cache_get)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_set", _fake_gen_cache_set)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_set", _fake_exp_cache_set)
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_context", _fake_adjacent_context)

    payload = literature_api.ReaderExperiencePlanRequest(
        page=7,
        focus_page=7,
        selected_kb_id=84,
        user_intent="Create a paper experience",
        reader_profile="curious_generalist",
    )

    with pytest.raises(HTTPException) as exc:
        await literature_api.get_reader_experience_plan_cached(
            paper_id=78,
            payload=payload,
            db=SimpleNamespace(),
            current_user=SimpleNamespace(id=5),
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "No completed experience manuscript cached for this page"

    full_response = await literature_api.get_reader_experience_plan(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert full_response.generative_plan.status == "done"
    assert full_response.plan.page_story_title == "Full experience"
    assert full_response.generative_plan_cache_hit is False
    assert full_response.experience_cache_hit is False
    assert captured["build_plan_calls"] == 1

    cached_response = await literature_api.get_reader_experience_plan_cached(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert cached_response.generative_plan_cache_hit is True
    assert cached_response.experience_cache_hit is True
    assert cached_response.generative_plan.status == "done"
    assert cached_response.plan.page_story_title == "Full experience"
    assert captured["build_plan_calls"] == 1


@pytest.mark.asyncio
async def test_list_literature_ask_sessions_returns_user_scoped_rows():
    now = datetime.now(timezone.utc)
    rows = [
        SimpleNamespace(
            id=1,
            user_id=7,
            scope="paper",
            paper_id=100,
            collection_id=None,
            knowledge_base_id=3,
            title="会话1",
            created_at=now,
            updated_at=now,
        )
    ]
    db = _FakeDB([_FakeResult(rows=rows)])

    result = await literature_api.list_literature_ask_sessions(
        scope=None,
        paper_id=None,
        collection_id=None,
        knowledge_base_id=None,
        limit=30,
        offset=0,
        db=db,
        current_user=SimpleNamespace(id=7),
    )

    assert len(result) == 1
    assert result[0].id == 1
    assert result[0].scope == "paper"


@pytest.mark.asyncio
async def test_list_literature_ask_messages_filters_invalid_sources():
    now = datetime.now(timezone.utc)
    session = SimpleNamespace(id=8, user_id=7)
    rows = [
        SimpleNamespace(
            id=11,
            session_id=8,
            role="assistant",
            content="answer",
            sources=[
                "bad-source",
                {
                    "document_id": 1,
                    "document_name": "paper.pdf",
                    "snippet": "snippet",
                    "score": 0.91,
                },
            ],
            created_at=now,
        )
    ]
    db = _FakeDB([_FakeResult(row=session), _FakeResult(rows=rows)])

    result = await literature_api.list_literature_ask_messages(
        session_id=8,
        limit=200,
        offset=0,
        db=db,
        current_user=SimpleNamespace(id=7),
    )

    assert len(result) == 1
    assert result[0].role == "assistant"
    assert result[0].sources[0].document_name == "paper.pdf"


@pytest.mark.asyncio
async def test_list_literature_ask_messages_raises_404_for_unknown_session():
    db = _FakeDB([_FakeResult(row=None)])

    with pytest.raises(HTTPException) as exc:
        await literature_api.list_literature_ask_messages(
            session_id=999,
            limit=200,
            offset=0,
            db=db,
            current_user=SimpleNamespace(id=7),
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_build_generative_reader_agent_tool_registry_for_paper_should_keep_web_mcp_tools(monkeypatch, tmp_path: Path):
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%EOF\n")
    paper = SimpleNamespace(id=78, title="Demo Paper", knowledge_base_id=None)

    class _FakeTool:
        def __init__(self, name: str):
            self.name = name

    class _FakeRegistry:
        def __init__(self, *args, **kwargs):
            self._tools = {
                "web_search": _FakeTool("web_search"),
                "web_scrape": _FakeTool("web_scrape"),
                "calculator": _FakeTool("calculator"),
            }
            self._mcp_tools = {
                "mcp.brave.search": _FakeTool("mcp.brave.search"),
                "mcp.firecrawl.firecrawl_scrape": _FakeTool("mcp.firecrawl.firecrawl_scrape"),
                "mcp.filesystem.read_file": _FakeTool("mcp.filesystem.read_file"),
            }

        def register(self, tool):
            self._tools[tool.name] = tool

        async def refresh_mcp_tools(self, force_refresh: bool = False):
            return None

        def list_tools(self):
            return [
                {"function": {"name": name}}
                for name in [*self._tools.keys(), *self._mcp_tools.keys()]
            ]

    monkeypatch.setattr(literature_api, "ToolRegistry", _FakeRegistry)
    monkeypatch.setattr(literature_api, "_resolve_local_pdf_path", lambda _user_id, _paper: str(pdf_path))
    monkeypatch.setattr(
        literature_api,
        "LiteratureDirectPaperReadTool",
        lambda **kwargs: _FakeTool("paper_read"),
    )

    registry, allowed = await literature_api._build_generative_reader_agent_tool_registry_for_paper(
        db=None,
        current_user=SimpleNamespace(id=5),
        paper=paper,
        selected_kb_id=None,
    )

    assert registry is not None
    assert allowed == {
        "paper_read",
        "web_search",
        "web_scrape",
        "mcp.brave.search",
        "mcp.firecrawl.firecrawl_scrape",
    }


def test_mcp_client_normalize_call_result_should_shape_web_search_payload():
    schema = MCPToolSchema(
        server_name="tavily",
        tool_name="tavily_search",
        qualified_name="mcp.tavily.tavily_search",
        description="search the web",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
    )
    call_result = SimpleNamespace(
        content=[],
        isError=False,
        structuredContent={
            "success": True,
            "output": "search ok",
            "data": {
                "results": [
                    {
                        "title": "USMLE Overview",
                        "url": "https://www.usmle.org/",
                        "content": "Official overview.",
                    }
                ]
            },
        },
    )

    result = MCPClientManager._normalize_call_result(
        call_result,
        schema=schema,
        arguments={"query": "usmle structure"},
    )

    assert result.success is True
    assert result.output == "search ok"
    assert result.data["results"][0]["snippet"] == "Official overview."
    assert result.data["results"][0]["rank"] == 1
    assert result.data["results"][0]["domain"] == "usmle.org"
    assert result.data["reader_summary"]
    assert result.data["source_kind"] == "public_web_search"
    assert result.data["structured_content"]["results"][0]["url"] == "https://www.usmle.org/"
    assert result.data["structured_content"]["domains"][0]["domain"] == "usmle.org"
    assert result.data["public_links"][0]["href"] == "https://www.usmle.org/"
    assert result.data["provider"] == "tavily"
    assert result.data["provider_route"] == "mcp.tavily.tavily_search"
    assert result.data["provenance"]["execution_mode"] == "direct"
    assert result.data["provenance"]["tool_kind"] == "web_search"


def test_mcp_client_normalize_call_result_should_shape_web_scrape_payload():
    schema = MCPToolSchema(
        server_name="fetch",
        tool_name="fetch",
        qualified_name="mcp.fetch.fetch",
        description="fetch a page",
        input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
    )
    call_result = SimpleNamespace(
        content=[],
        isError=False,
        structuredContent={
            "metadata": {"title": "Example Domain"},
            "markdown": "# Example Domain\nThis domain is for use in illustrative examples.",
        },
    )

    result = MCPClientManager._normalize_call_result(
        call_result,
        schema=schema,
        arguments={"url": "https://example.com", "formats": ["markdown"]},
    )

    assert result.success is True
    assert result.output.startswith("# Example Domain")
    assert result.data["url"] == "https://example.com"
    assert result.data["structured_content"]["metadata"]["title"] == "Example Domain"
    assert result.data["reader_summary"].startswith("# Example Domain")
    assert result.data["source_domain"] == "example.com"
    assert result.data["source_kind"] == "public_web_page"
    assert result.data["public_links"][0]["href"] == "https://example.com"
    assert result.data["provider"] == "fetch"
    assert result.data["provenance"]["tool_kind"] == "web_scrape"


@pytest.mark.asyncio
async def test_tool_registry_should_translate_routed_web_search_arguments_and_preserve_provenance(monkeypatch):
    local_search = _CountingTool("web_search", output="local-web")
    fake_manager = _FakeRoutedMCPManager(
        schemas=[
            MCPToolSchema(
                server_name="brave",
                tool_name="search",
                qualified_name="mcp.brave.search",
                description="search",
                input_schema={
                    "type": "object",
                    "properties": {
                        "q": {"type": "string"},
                        "count": {"type": "integer"},
                    },
                },
            )
        ],
        responses={
            "mcp.brave.search": SimpleNamespace(
                success=True,
                output="remote-web",
                data={
                    "structured_content": {
                        "results": [
                            {
                                "title": "USMLE Overview",
                                "url": "https://www.usmle.org/",
                                "snippet": "Official overview.",
                            }
                        ]
                    }
                },
                error=None,
            )
        },
    )

    _patch_registry_default_tools(monkeypatch, web_search_tool=local_search)
    monkeypatch.setattr(agent_tools.settings, "mcp_enabled", True)
    monkeypatch.setattr(agent_tools.settings, "mcp_tool_routes", '{"web_search": ["mcp.brave.search"]}')
    monkeypatch.setattr(agent_tools.settings, "mcp_route_timeout_seconds", 3)
    monkeypatch.setattr(agent_tools.settings, "mcp_route_retry_attempts", 1)
    monkeypatch.setattr(agent_tools.ToolRegistry, "_create_mcp_client_manager", lambda self: fake_manager)
    agent_tools.ToolRegistry._mcp_route_circuit_state = {}

    registry = agent_tools.ToolRegistry(db=None, user_id=1)
    result = await registry.execute("web_search", query="llm news", max_results=3)

    assert result.success is True
    assert result.output == "remote-web"
    assert local_search.calls == []
    assert fake_manager.call_history == [("mcp.brave.search", {"q": "llm news", "count": 3})]
    assert result.data["provider"] == "brave"
    assert result.data["provider_route"] == "mcp.brave.search"
    assert result.data["source_kind"] == "public_web_search"
    assert result.data["reader_summary"] == "USMLE Overview: Official overview."
    assert result.data["provenance"]["execution_mode"] == "routed"
    assert result.data["provenance"]["local_tool_name"] == "web_search"
    assert result.data["provenance"]["argument_translation"]["translated_arguments"] == {
        "q": "llm news",
        "count": 3,
    }


@pytest.mark.asyncio
async def test_tool_registry_should_translate_routed_web_scrape_arguments_for_extract_routes(monkeypatch):
    local_scrape = _CountingTool("web_scrape", output="local-scrape")
    fake_manager = _FakeRoutedMCPManager(
        schemas=[
            MCPToolSchema(
                server_name="firecrawl",
                tool_name="firecrawl_extract",
                qualified_name="mcp.firecrawl.firecrawl_extract",
                description="extract content",
                input_schema={
                    "type": "object",
                    "properties": {
                        "urls": {"type": "array"},
                        "prompt": {"type": "string"},
                        "format": {"type": "string", "enum": ["markdown", "html"]},
                    },
                    "required": ["urls", "prompt"],
                },
            )
        ],
        responses={
            "mcp.firecrawl.firecrawl_extract": SimpleNamespace(
                success=True,
                output="Reader-facing summary",
                data={"structured_content": {"metadata": {"title": "Example Domain"}}},
                error=None,
            )
        },
    )

    _patch_registry_default_tools(monkeypatch, web_scrape_tool=local_scrape)
    monkeypatch.setattr(agent_tools.settings, "mcp_enabled", True)
    monkeypatch.setattr(agent_tools.settings, "mcp_tool_routes", '{"web_scrape": ["mcp.firecrawl.firecrawl_extract"]}')
    monkeypatch.setattr(agent_tools.settings, "mcp_route_timeout_seconds", 3)
    monkeypatch.setattr(agent_tools.settings, "mcp_route_retry_attempts", 1)
    monkeypatch.setattr(agent_tools.ToolRegistry, "_create_mcp_client_manager", lambda self: fake_manager)
    agent_tools.ToolRegistry._mcp_route_circuit_state = {}

    registry = agent_tools.ToolRegistry(db=None, user_id=1)
    result = await registry.execute(
        "web_scrape",
        url="https://example.com",
        formats=["markdown"],
        only_main_content=True,
    )

    assert result.success is True
    assert result.output == "Reader-facing summary"
    assert local_scrape.calls == []
    assert fake_manager.call_history[0][0] == "mcp.firecrawl.firecrawl_extract"
    assert fake_manager.call_history[0][1]["urls"] == ["https://example.com"]
    assert fake_manager.call_history[0][1]["format"] == "markdown"
    assert "main article content" in fake_manager.call_history[0][1]["prompt"]
    assert result.data["provider"] == "firecrawl"
    assert result.data["provider_route"] == "mcp.firecrawl.firecrawl_extract"
    assert result.data["source_kind"] == "public_web_page"
    assert result.data["provenance"]["execution_mode"] == "routed"
    assert result.data["provenance"]["local_tool_name"] == "web_scrape"
    assert result.data["provenance"]["argument_translation"]["applied"] is True


def test_derive_link_status_from_document_completed():
    doc = SimpleNamespace(id=123, status=DocumentStatus.COMPLETED.value, error_message=None)
    status, error_message, doc_id = literature_api._derive_link_status_from_document(doc)
    assert status == KnowledgeLinkStatus.COMPLETED.value
    assert error_message is None
    assert doc_id == 123


def test_derive_link_status_from_document_processing_clears_error():
    doc = SimpleNamespace(id=9, status=DocumentStatus.RUNNING.value, error_message="old error")
    status, error_message, doc_id = literature_api._derive_link_status_from_document(doc)
    assert status == KnowledgeLinkStatus.RUNNING.value
    assert error_message is None
    assert doc_id == 9


def test_mark_stale_document_timeout_marks_processing_doc_as_timeout(monkeypatch):
    monkeypatch.setattr(literature_api.settings, "document_processing_stale_timeout_seconds", 60)
    doc = SimpleNamespace(
        id=130,
        status=DocumentStatus.RUNNING.value,
        error_message=None,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        updated_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    changed = literature_api._mark_stale_document_timeout(doc)

    assert changed is True
    assert doc.status == DocumentStatus.TIMEOUT.value
    assert "文档处理超时" in doc.error_message


def test_normalize_collection_name_repairs_known_mojibake_tokens():
    for token in literature_api._build_mojibake_variants("所有论文"):
        assert literature_api._normalize_collection_name(token) == "所有论文"
    for token in literature_api._build_mojibake_variants("待读"):
        assert literature_api._normalize_collection_name(token) == "待读"
    for token in literature_api._build_mojibake_variants("已读"):
        assert literature_api._normalize_collection_name(token) == "已读"
    for token in literature_api._build_mojibake_variants("收藏"):
        assert literature_api._normalize_collection_name(token) == "收藏"
    assert literature_api._normalize_collection_name("我的收藏") == "我的收藏"


def test_normalize_collection_description_repairs_known_mojibake_tokens():
    for token in literature_api._build_mojibake_variants("所有保存的论文"):
        assert literature_api._normalize_collection_description(token) == "所有保存的论文"
    for token in literature_api._build_mojibake_variants("待阅读的论文"):
        assert literature_api._normalize_collection_description(token) == "待阅读的论文"
    for token in literature_api._build_mojibake_variants("已阅读的论文"):
        assert literature_api._normalize_collection_description(token) == "已阅读的论文"
    for token in literature_api._build_mojibake_variants("重要论文"):
        assert literature_api._normalize_collection_description(token) == "重要论文"
    assert literature_api._normalize_collection_description("用户自定义描述") == "用户自定义描述"


def _resolve_reading_dossier_v2_builder():
    for name in (
        "_build_reading_dossier_v2",
        "_build_experience_reading_dossier_v2",
        "_build_experience_page_dossier_v2",
    ):
        fn = getattr(literature_api, name, None)
        if callable(fn):
            return fn
    pytest.fail("reading_dossier_v2 helper was not found on app.api.literature")


def _invoke_reading_dossier_v2_builder(
    *,
    focus_page: int,
    reader_profile: str,
    compose_payload: dict,
    adjacent_page_context: list[dict],
    compose_source_signature: str,
    source_sig_hash: str,
):
    builder = _resolve_reading_dossier_v2_builder()
    call_args = {
        "focus_page": int(focus_page),
        "page": int(focus_page),
        "reader_profile": str(reader_profile),
        "compose_payload": dict(compose_payload),
        "adjacent_page_context": list(adjacent_page_context),
        "compose_source_signature": str(compose_source_signature),
        "source_signature": str(compose_source_signature),
        "source_sig_hash": str(source_sig_hash),
    }
    accepted = inspect.signature(builder).parameters
    kwargs = {key: value for key, value in call_args.items() if key in accepted}
    dossier = builder(**kwargs)
    assert isinstance(dossier, dict)
    return dossier


def _build_sample_compose_payload_for_dossier_v2() -> dict:
    return {
        "status": "done",
        "build_mode": "compose_agent_simplified",
        "pipeline_version": "layout_uid_v1",
        "source_signature": "compose-sig",
        "assets": [
            {
                "kind": "image_hint",
                "label": "Figure layout:7:fig1",
                "source": "pdf",
                "href": "http://localhost:8888/api/v1/literature/reader/figure-assets/78/7/layout_7_fig1",
                "meta": {
                    "asset_id": "layout_7_fig1",
                    "layout_unique_id": "layout_7_fig1",
                    "layout_id": "layout:7:fig1",
                    "page": 7,
                },
            }
        ],
        "page_grounding_v1": {
            "version": "page_grounding_v1",
            "page": 7,
            "layout_atoms": [
                {
                    "layout_id": "layout:7:1",
                    "reading_order": 1,
                    "node_kind": "paragraph",
                    "layout_type": "paragraph",
                    "layout_sub_type": "body",
                    "clean_text": "We first examined the frequency of insight.",
                    "normalized_text": "we first examined the frequency of insight.",
                    "normalization_reason": "trim",
                    "normalization_mode": "light",
                    "normalization_confidence": 0.92,
                    "include_in_main_flow": True,
                    "source_block_ids": ["blk-7-1"],
                },
                {
                    "layout_id": "layout:7:fig1",
                    "reading_order": 2,
                    "node_kind": "figure",
                    "layout_type": "figure",
                    "layout_sub_type": "panel",
                    "clean_text": "Figure 3 concordance panel",
                    "include_in_main_flow": False,
                    "canonical_block_ids": ["blk-7-fig1"],
                },
                {
                    "layout_id": "layout:7:tbl1",
                    "reading_order": 3,
                    "node_kind": "table",
                    "layout_type": "table",
                    "layout_sub_type": "results",
                    "clean_text": "Table of score breakdown",
                    "include_in_main_flow": False,
                    "canonical_block_ids": ["blk-7-tbl1"],
                    "table_cells": [
                        {"row_start": 0, "col_start": 0, "text": "Metric"},
                        {"row_start": 0, "col_start": 1, "text": "Score"},
                        {"row_start": 1, "col_start": 0, "text": "Insight"},
                        {"row_start": 1, "col_start": 1, "text": "0.81"},
                    ],
                },
                {
                    "layout_id": "layout:7:eq1",
                    "reading_order": 4,
                    "node_kind": "equation",
                    "layout_type": "equation",
                    "layout_sub_type": "display",
                    "clean_text": "score = a + b",
                    "normalized_text": "score = a + b",
                    "include_in_main_flow": False,
                    "canonical_block_ids": ["blk-7-eq1"],
                },
            ],
            "reading_nodes": [
                {
                    "node_id": "node:7:1",
                    "node_kind": "paragraph",
                    "clean_text": "We first examined the frequency of insight.",
                    "normalized_text": "we first examined the frequency of insight.",
                    "source_layout_ids": ["layout:7:1"],
                    "source_block_ids": ["blk-7-1"],
                    "include_in_main_flow": True,
                }
            ],
            "evidence_map": [
                {
                    "evidence_id": "ev-7-1",
                    "source_layout_id": "layout:7:1",
                    "source_block_ids": ["blk-7-1"],
                    "layout_pos": [{"x": 0.1, "y": 0.2}],
                    "block_positions": [[{"x": 0.1, "y": 0.2}]],
                    "table_cells": [],
                },
                {
                    "evidence_id": "ev-7-fig1",
                    "source_layout_id": "layout:7:fig1",
                    "source_block_ids": ["blk-7-fig1"],
                },
                {
                    "evidence_id": "ev-7-tbl1",
                    "source_layout_id": "layout:7:tbl1",
                    "source_block_ids": ["blk-7-tbl1"],
                },
                {
                    "evidence_id": "ev-7-eq1",
                    "source_layout_id": "layout:7:eq1",
                    "source_block_ids": ["blk-7-eq1"],
                },
            ],
            "page_image": {
                "url": "https://example.com/p7.png",
                "width": 1200,
                "height": 1800,
            },
        },
    }


def _build_sample_adjacent_structured_context_for_dossier_v2() -> list[dict]:
    return [
        {
            "page": 6,
            "relation": "previous_page",
            "source": "neighbor_page_vlm_parse",
            "fidelity": "ordered_structured_context",
            "reference_only": False,
            "page_image": {"url": "https://example.com/p6.png", "width": 1200, "height": 1800},
            "page_summary": "Previous page sets up the figure comparison.",
            "content_stream": [
                {"seq": 1, "type": "header", "text": "Results"},
                {"seq": 2, "type": "paragraph", "text": "The analysis begins by comparing concordance rates.", "ocr_text": "The analysis begins by comparing concordance rates.", "role": "body"},
                {"seq": 3, "type": "figure", "label": "Figure 2", "caption": "Earlier concordance view", "description": "A prior chart foreshadowing Figure 3.", "ocr_text": "Figure 2"},
            ],
            "continuation_hints": ["Figure 3 continues the comparison introduced on the previous page."],
            "raw_text": "Results. The analysis begins by comparing concordance rates. Figure 2 ...",
            "meta": {"capture": "test"},
        },
        {
            "page": 8,
            "relation": "next_page",
            "source": "neighbor_page_vlm_parse",
            "fidelity": "ordered_structured_context",
            "reference_only": False,
            "page_image": {"url": "https://example.com/p8.png", "width": 1200, "height": 1800},
            "page_summary": "Next page expands the score table.",
            "content_stream": [
                {"seq": 1, "type": "paragraph", "text": "The next page interprets the table row by row.", "ocr_text": "The next page interprets the table row by row.", "role": "body"},
                {"seq": 2, "type": "table", "label": "Table 2", "caption": "Extended score table", "description": "Structured score comparison", "columns": ["Metric", "Score"], "rows": [["Insight", "0.81"], ["Accuracy", "0.74"]]},
                {"seq": 3, "type": "footer", "text": "Supplementary discussion"},
            ],
            "continuation_hints": ["The table explanation continues on the next page."],
            "raw_text": "The next page interprets the table row by row. Table 2 ...",
            "meta": {"capture": "test"},
        },
    ]


def test_reading_dossier_v2_should_preserve_current_page_rich_grounding_from_compose_payload():
    compose_payload = _build_sample_compose_payload_for_dossier_v2()
    dossier = _invoke_reading_dossier_v2_builder(
        focus_page=7,
        reader_profile="curious_generalist",
        compose_payload=compose_payload,
        adjacent_page_context=[],
        compose_source_signature="compose-sig",
        source_sig_hash="sig-hash-v2",
    )

    assert dossier["version"] == "reading_dossier_v2"
    assert dossier["current_page"]["owner"] == "compose/page_grounding_v1"
    assert dossier["current_page"]["fidelity"] == "grounded_evidence"
    assert dossier["current_page"]["rich_grounding"]["page"] == 7
    assert dossier["current_page"]["rich_grounding"]["layout_atoms"][0]["layout_id"] == "layout:7:1"
    assert dossier["current_page"]["rich_grounding"]["evidence_map"][0]["source_layout_id"] == "layout:7:1"
    assert dossier["current_page"]["rich_grounding"]["page_image"]["url"] == "https://example.com/p7.png"


def test_reading_dossier_v2_should_mark_current_page_degraded_when_page_grounding_missing():
    compose_payload = _build_sample_compose_payload_for_dossier_v2()
    compose_payload.pop("page_grounding_v1", None)
    dossier = _invoke_reading_dossier_v2_builder(
        focus_page=7,
        reader_profile="curious_generalist",
        compose_payload=compose_payload,
        adjacent_page_context=[],
        compose_source_signature="compose-sig",
        source_sig_hash="sig-hash-v2",
    )

    assert dossier["current_page"]["build_meta"]["degraded"] is True
    assert dossier["current_page"]["build_meta"]["degraded_reason"] == "missing_page_grounding_v1"
    assert dossier["current_page"]["rich_grounding"]["page"] == 7
    assert dossier["current_page"]["rich_grounding"]["layout_atoms"] == []
    assert dossier["current_page"]["rich_grounding"]["evidence_map"] == []
    assert dossier["meta"]["current_page_grounding_degraded"] is True
    assert dossier["meta"]["current_page_grounding_degraded_reason"] == "missing_page_grounding_v1"


def test_reading_dossier_v2_should_preserve_ordered_structured_adjacent_pages_lane():
    compose_payload = _build_sample_compose_payload_for_dossier_v2()
    adjacent_page_context = _build_sample_adjacent_structured_context_for_dossier_v2()
    dossier = _invoke_reading_dossier_v2_builder(
        focus_page=7,
        reader_profile="curious_generalist",
        compose_payload=compose_payload,
        adjacent_page_context=adjacent_page_context,
        compose_source_signature="compose-sig",
        source_sig_hash="sig-hash-v2",
    )

    assert dossier["adjacent_pages"]["owner"] == "api/adjacent_page_extraction"
    assert dossier["adjacent_pages"]["fidelity"] == "ordered_structured_context"
    assert dossier["adjacent_pages"]["limits"]["reference_only"] is False
    assert [row["page"] for row in dossier["adjacent_pages"]["pages"]] == [6, 8]
    assert all(row["reference_only"] is False for row in dossier["adjacent_pages"]["pages"])
    assert dossier["adjacent_pages"]["pages"][0]["content_stream"][0]["seq"] == 1
    assert dossier["adjacent_pages"]["pages"][0]["content_stream"][1]["seq"] == 2
    assert dossier["adjacent_pages"]["pages"][0]["content_stream"][1]["type"] == "paragraph"
    assert dossier["adjacent_pages"]["pages"][1]["content_stream"][1]["type"] == "table"
    assert "adjacent_page_context" not in dossier["current_page"]


def test_reading_dossier_v2_should_reject_fake_ordered_structured_context_without_content_stream():
    compose_payload = _build_sample_compose_payload_for_dossier_v2()
    adjacent_page_context = [
        {
            "page": 6,
            "relation": "previous_page",
            "source": "neighbor_page_vlm_parse",
            "fidelity": "ordered_structured_context",
            "reference_only": False,
            "page_image": {"url": "https://example.com/p6.png"},
            "page_summary": "This looks structured but is not.",
            "summary": "legacy compact summary",
            "body_text": "legacy compact body_text",
            "continuation_hints": ["legacy"],
        }
    ]

    with pytest.raises(ValueError, match="ordered_structured_context cannot be built from compact summary fields"):
        _invoke_reading_dossier_v2_builder(
            focus_page=7,
            reader_profile="curious_generalist",
            compose_payload=compose_payload,
            adjacent_page_context=adjacent_page_context,
            compose_source_signature="compose-sig",
            source_sig_hash="sig-hash-v2",
        )


def test_reading_dossier_v2_should_reject_legacy_json_payload_stuffed_into_content_stream_text():
    compose_payload = _build_sample_compose_payload_for_dossier_v2()
    adjacent_page_context = [
        {
            "page": 6,
            "relation": "previous_page",
            "source": "neighbor_page_vlm_parse",
            "fidelity": "ordered_structured_context",
            "reference_only": False,
            "page_image": {"url": "https://example.com/p6.png"},
            "page_summary": "Looks ordered but actually embeds a legacy payload.",
            "content_stream": [
                {
                    "seq": 1,
                    "type": "paragraph",
                    "text": '{"page":6,"summary":"legacy compact summary","body_text":"legacy compact body","continuation_hints":["legacy"]}',
                }
            ],
            "continuation_hints": ["legacy"],
            "raw_text": "legacy raw",
        }
    ]

    with pytest.raises(ValueError, match="legacy JSON payload stuffing"):
        _invoke_reading_dossier_v2_builder(
            focus_page=7,
            reader_profile="curious_generalist",
            compose_payload=compose_payload,
            adjacent_page_context=adjacent_page_context,
            compose_source_signature="compose-sig",
            source_sig_hash="sig-hash-v2",
        )


@pytest.mark.asyncio
async def test_extract_adjacent_page_structured_context_v2_should_retry_primary_model_with_full_token_budget(
    tmp_path,
    monkeypatch,
):
    image_path = tmp_path / "page-6.jpg"
    image_path.write_bytes(b"fake-image")
    expected_row = _build_sample_adjacent_structured_context_for_dossier_v2()[0]
    calls: list[tuple[str, int]] = []

    async def _fake_chat_json(**kwargs):
        calls.append((str(kwargs.get("model") or ""), int(kwargs.get("max_tokens") or 0)))
        if len(calls) == 1:
            return {
                "parsed": {},
                "raw_text": '{"page":6,"content_stream":[',
                "usage": {"completion_tokens": 7000},
            }
        return {
            "parsed": json.loads(json.dumps(expected_row, ensure_ascii=False)),
            "raw_text": json.dumps(expected_row, ensure_ascii=False),
            "usage": {"completion_tokens": 420},
        }

    monkeypatch.setattr(literature_api.DashScopeMultimodalService, "is_available", staticmethod(lambda: True))
    monkeypatch.setattr(literature_api.DashScopeMultimodalService, "chat_json", _fake_chat_json)
    monkeypatch.setattr(literature_api.settings, "aliyun_api_key", "test-key", raising=False)
    monkeypatch.setattr(literature_api.settings, "aliyun_dashscope_api_base", "https://dashscope.example/api/v1", raising=False)
    monkeypatch.setattr(literature_api.settings, "reader_mm_parser_model", "qwen3-vl-flash", raising=False)
    monkeypatch.setattr(literature_api.settings, "reader_mm_fallback_model", "qwen3-vl-plus", raising=False)
    monkeypatch.setattr(literature_api.settings, "reader_mm_parser_max_tokens", 7000, raising=False)

    row = await literature_api._extract_adjacent_page_structured_context_v2(
        image_path=str(image_path),
        relation="previous_page",
        page=6,
        image_url="https://example.com/p6.png",
    )

    assert row["page"] == 6
    assert row["content_stream"][0]["seq"] == 1
    assert calls == [("qwen3-vl-flash", 7000), ("qwen3-vl-flash", 7000)]
    assert row["meta"]["parser_model"] == "qwen3-vl-flash"
    assert row["meta"]["attempt_index"] == 2


@pytest.mark.asyncio
async def test_extract_adjacent_page_structured_context_v2_should_not_escalate_to_fallback_model(
    tmp_path,
    monkeypatch,
):
    image_path = tmp_path / "page-8.jpg"
    image_path.write_bytes(b"fake-image")
    calls: list[str] = []

    async def _fake_chat_json(**kwargs):
        model = str(kwargs.get("model") or "")
        calls.append(model)
        return {
            "parsed": {},
            "raw_text": '{"page":8,"page_summary":"truncated"',
            "usage": {"completion_tokens": 7000},
        }

    monkeypatch.setattr(literature_api.DashScopeMultimodalService, "is_available", staticmethod(lambda: True))
    monkeypatch.setattr(literature_api.DashScopeMultimodalService, "chat_json", _fake_chat_json)
    monkeypatch.setattr(literature_api.settings, "aliyun_api_key", "test-key", raising=False)
    monkeypatch.setattr(literature_api.settings, "aliyun_dashscope_api_base", "https://dashscope.example/api/v1", raising=False)
    monkeypatch.setattr(literature_api.settings, "reader_mm_parser_model", "qwen3-vl-flash", raising=False)
    monkeypatch.setattr(literature_api.settings, "reader_mm_fallback_model", "qwen3-vl-plus", raising=False)
    monkeypatch.setattr(literature_api.settings, "reader_mm_parser_max_tokens", 7000, raising=False)

    with pytest.raises(ValueError, match="neighboring-page structured context generation failed after explicit model attempts"):
        await literature_api._extract_adjacent_page_structured_context_v2(
            image_path=str(image_path),
            relation="next_page",
            page=8,
            image_url="https://example.com/p8.png",
        )

    assert calls == ["qwen3-vl-flash", "qwen3-vl-flash", "qwen3-vl-flash"]


@pytest.mark.asyncio
async def test_extract_adjacent_page_structured_context_v2_should_fail_loudly_after_all_model_attempts(
    tmp_path,
    monkeypatch,
):
    image_path = tmp_path / "page-fail.jpg"
    image_path.write_bytes(b"fake-image")

    async def _fake_chat_json(**kwargs):
        return {
            "parsed": {},
            "raw_text": '{"page":6,"content_stream":[',
            "usage": {"completion_tokens": 7000},
        }

    monkeypatch.setattr(literature_api.DashScopeMultimodalService, "is_available", staticmethod(lambda: True))
    monkeypatch.setattr(literature_api.DashScopeMultimodalService, "chat_json", _fake_chat_json)
    monkeypatch.setattr(literature_api.settings, "aliyun_api_key", "test-key", raising=False)
    monkeypatch.setattr(literature_api.settings, "aliyun_dashscope_api_base", "https://dashscope.example/api/v1", raising=False)
    monkeypatch.setattr(literature_api.settings, "reader_mm_parser_model", "qwen3-vl-flash", raising=False)
    monkeypatch.setattr(literature_api.settings, "reader_mm_fallback_model", "qwen3-vl-plus", raising=False)
    monkeypatch.setattr(literature_api.settings, "reader_mm_parser_max_tokens", 7000, raising=False)

    with pytest.raises(ValueError, match="neighboring-page structured context generation failed after explicit model attempts"):
        await literature_api._extract_adjacent_page_structured_context_v2(
            image_path=str(image_path),
            relation="previous_page",
            page=6,
            image_url="https://example.com/p6.png",
        )


@pytest.mark.asyncio
async def test_build_experience_adjacent_page_structured_context_v2_should_reuse_page_scoped_structured_cache(
    tmp_path,
    monkeypatch,
):
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    page6 = tmp_path / "page-6.png"
    page6.write_bytes(b"page-6")
    page8 = tmp_path / "page-8.png"
    page8.write_bytes(b"page-8")

    sample_rows = {row["page"]: row for row in _build_sample_adjacent_structured_context_for_dossier_v2()}
    calls: list[tuple[int, str]] = []
    cache_store: dict[str, dict] = {}

    class _FakeReaderService:
        @staticmethod
        def _resolve_local_pdf_path(**_kwargs):
            return str(pdf_path)

    class _FakeComposeService:
        _reader_service = _FakeReaderService()

        @staticmethod
        async def ensure_page_render_asset(*, page: int, **_kwargs):
            return f"https://example.com/p{page}.png"

        @staticmethod
        def _find_existing_page_render_asset_path(*, page: int, **_kwargs):
            return str(page6 if page == 6 else page8)

    async def _fake_get_page_count(_pdf_path):
        return 8

    async def _fake_extract(**kwargs):
        page = int(kwargs["page"])
        relation = str(kwargs["relation"])
        calls.append((page, relation))
        return json.loads(json.dumps(sample_rows[page], ensure_ascii=False))

    async def _fake_cache_get(cache_key):
        if cache_key in cache_store:
            return json.loads(json.dumps(cache_store[cache_key], ensure_ascii=False)), "memory"
        return None, "none"

    async def _fake_cache_set(cache_key, payload, **_kwargs):
        cache_store[cache_key] = json.loads(json.dumps(payload, ensure_ascii=False))

    monkeypatch.setattr(literature_api, "_get_pdf_page_count", _fake_get_page_count)
    monkeypatch.setattr(literature_api, "_extract_adjacent_page_structured_context_v2", _fake_extract)
    monkeypatch.setattr(literature_api, "_adjacent_page_structured_v2_cache_get", _fake_cache_get)
    monkeypatch.setattr(literature_api, "_adjacent_page_structured_v2_cache_set", _fake_cache_set)

    first = await literature_api._build_experience_adjacent_page_structured_context_v2(
        compose_service=_FakeComposeService(),
        paper=SimpleNamespace(id=78, user_id=5, title="Demo Paper", pdf_path="paper.pdf"),
        focus_page=7,
        current_user=SimpleNamespace(id=5),
    )
    second = await literature_api._build_experience_adjacent_page_structured_context_v2(
        compose_service=_FakeComposeService(),
        paper=SimpleNamespace(id=78, user_id=5, title="Demo Paper", pdf_path="paper.pdf"),
        focus_page=7,
        current_user=SimpleNamespace(id=5),
    )

    assert calls == [(6, "previous_page"), (8, "next_page")]
    assert [row["page"] for row in first] == [6, 8]
    assert [row["page"] for row in second] == [6, 8]
    assert all(str((row.get("meta") or {}).get("page_scope_cache_layer") or "").strip() == "memory" for row in second)


@pytest.mark.asyncio
async def test_build_experience_adjacent_page_structured_context_v2_should_fail_loudly_when_cached_row_is_invalid(
    tmp_path,
    monkeypatch,
):
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    page6 = tmp_path / "page-6.png"
    page6.write_bytes(b"page-6")

    class _FakeReaderService:
        @staticmethod
        def _resolve_local_pdf_path(**_kwargs):
            return str(pdf_path)

    class _FakeComposeService:
        _reader_service = _FakeReaderService()

        @staticmethod
        async def ensure_page_render_asset(*, page: int, **_kwargs):
            return f"https://example.com/p{page}.png"

        @staticmethod
        def _find_existing_page_render_asset_path(*, page: int, **_kwargs):
            return str(page6)

    async def _fake_get_page_count(_pdf_path):
        return 8

    async def _fake_cache_get(_cache_key):
        return {
            "page": 6,
            "relation": "previous_page",
            "source": "neighbor_page_vlm_parse",
            "fidelity": "ordered_structured_context",
            "reference_only": False,
            "page_image": {"url": "https://example.com/p6.png"},
            "content_stream": [],
            "continuation_hints": [],
            "meta": {},
        }, "db"

    monkeypatch.setattr(literature_api, "_get_pdf_page_count", _fake_get_page_count)
    monkeypatch.setattr(literature_api, "_adjacent_page_structured_v2_cache_get", _fake_cache_get)

    with pytest.raises(ValueError, match="neighboring-page structured context cache corrupted for page 6: ordered content_stream missing"):
        await literature_api._build_experience_adjacent_page_structured_context_v2(
            compose_service=_FakeComposeService(),
            paper=SimpleNamespace(id=78, user_id=5, title="Demo Paper", pdf_path="paper.pdf"),
            focus_page=7,
            current_user=SimpleNamespace(id=5),
        )


def test_coerce_adjacent_page_structured_result_should_normalize_common_item_type_aliases():
    payload = {
        "parsed": {
            "page": 6,
            "relation": "previous_page",
            "source": "neighbor_page_vlm_parse",
            "fidelity": "ordered_structured_context",
            "reference_only": False,
            "page_summary": "summary",
            "content_stream": [
                {"seq": 1, "type": "heading", "text": "Section title"},
                {"seq": 2, "type": "link", "text": "https://example.com"},
                {"seq": 3, "type": "formula", "normalized_text": "a+b"},
            ],
            "continuation_hints": ["hint"],
            "raw_text": "raw",
            "meta": {},
        },
        "raw_text": "{}",
        "usage": {"completion_tokens": 300},
    }

    row = literature_api._coerce_adjacent_page_structured_result(
        result=payload,
        page=6,
        relation="previous_page",
        image_path="",
        image_url="https://example.com/p6.png",
    )

    assert [item["type"] for item in row["content_stream"]] == ["header", "paragraph", "equation"]
    assert row["content_stream"][0]["meta"]["raw_type"] == "heading"
    assert row["content_stream"][1]["meta"]["raw_type"] == "link"


def test_reading_dossier_v2_should_expose_cache_meta_with_v2_specific_namespace():
    compose_payload = _build_sample_compose_payload_for_dossier_v2()
    dossier = _invoke_reading_dossier_v2_builder(
        focus_page=7,
        reader_profile="curious_generalist",
        compose_payload=compose_payload,
        adjacent_page_context=[],
        compose_source_signature="compose-sig",
        source_sig_hash="sig-hash-v2",
    )

    assert dossier["cache_meta"]["compose_pipeline_version"] == "layout_uid_v1"
    assert dossier["cache_meta"]["source_sig_hash"] == "sig-hash-v2"
    assert dossier["cache_meta"]["adjacent_context_parser_version"] == "adjacent_parser:unknown"
    assert dossier["cache_meta"]["adjacent_context_sig_hash"]
    assert dossier["cache_meta"]["adjacent_context_page_scope_version"] == "ordered_structured_context.v1"
    assert "dossier" in dossier["cache_meta"]["dossier_namespace"]
    assert "v2" in dossier["cache_meta"]["dossier_namespace"]
    assert ":v33:" not in dossier["cache_meta"]["dossier_namespace"]


def test_reading_dossier_v2_signature_should_change_when_adjacent_structured_context_changes():
    compose_payload = _build_sample_compose_payload_for_dossier_v2()
    adjacent_context = _build_sample_adjacent_structured_context_for_dossier_v2()
    dossier_a = _invoke_reading_dossier_v2_builder(
        focus_page=7,
        reader_profile="curious_generalist",
        compose_payload=compose_payload,
        adjacent_page_context=adjacent_context,
        compose_source_signature="compose-sig",
        source_sig_hash="sig-hash-v2",
    )
    sig_a = literature_api._reading_dossier_v2_signature(dossier_a)

    adjacent_context_mutated = json.loads(json.dumps(adjacent_context, ensure_ascii=False))
    adjacent_context_mutated[0]["content_stream"][1]["text"] = "The analysis starts with revised concordance framing."
    dossier_b = _invoke_reading_dossier_v2_builder(
        focus_page=7,
        reader_profile="curious_generalist",
        compose_payload=compose_payload,
        adjacent_page_context=adjacent_context_mutated,
        compose_source_signature="compose-sig",
        source_sig_hash="sig-hash-v2",
    )
    sig_b = literature_api._reading_dossier_v2_signature(dossier_b)

    assert dossier_a["compose_source_signature"] == dossier_b["compose_source_signature"]
    assert dossier_a["cache_meta"]["adjacent_context_sig_hash"] != dossier_b["cache_meta"]["adjacent_context_sig_hash"]
    assert sig_a != sig_b


def test_experience_session_v2_cache_key_should_change_when_adjacent_parser_version_changes():
    compose_payload = _build_sample_compose_payload_for_dossier_v2()
    adjacent_context = _build_sample_adjacent_structured_context_for_dossier_v2()

    dossier_a = _invoke_reading_dossier_v2_builder(
        focus_page=7,
        reader_profile="curious_generalist",
        compose_payload=compose_payload,
        adjacent_page_context=adjacent_context,
        compose_source_signature="compose-sig",
        source_sig_hash="sig-hash-v2",
    )
    sig_a = literature_api._reading_dossier_v2_signature(dossier_a)
    key_a = literature_api._experience_session_v2_cache_key(
        user_id=5,
        paper_id=78,
        focus_page=7,
        selected_kb_id=0,
        dossier_signature=sig_a,
        user_intent="build experience",
        reader_profile="curious_generalist",
    )

    adjacent_context_mutated = json.loads(json.dumps(adjacent_context, ensure_ascii=False))
    adjacent_context_mutated[0]["meta"]["parser_version"] = "vlm-parser-v2.1"
    dossier_b = _invoke_reading_dossier_v2_builder(
        focus_page=7,
        reader_profile="curious_generalist",
        compose_payload=compose_payload,
        adjacent_page_context=adjacent_context_mutated,
        compose_source_signature="compose-sig",
        source_sig_hash="sig-hash-v2",
    )
    sig_b = literature_api._reading_dossier_v2_signature(dossier_b)
    key_b = literature_api._experience_session_v2_cache_key(
        user_id=5,
        paper_id=78,
        focus_page=7,
        selected_kb_id=0,
        dossier_signature=sig_b,
        user_intent="build experience",
        reader_profile="curious_generalist",
    )

    assert dossier_a["compose_source_signature"] == dossier_b["compose_source_signature"]
    assert dossier_a["cache_meta"]["source_sig_hash"] == dossier_b["cache_meta"]["source_sig_hash"]
    assert dossier_a["cache_meta"]["adjacent_context_parser_version"] != dossier_b["cache_meta"]["adjacent_context_parser_version"]
    assert sig_a != sig_b
    assert key_a != key_b


def _build_sample_reading_dossier_v2_for_session() -> dict:
    compose_payload = _build_sample_compose_payload_for_dossier_v2()
    return _invoke_reading_dossier_v2_builder(
        focus_page=7,
        reader_profile="curious_generalist",
        compose_payload=compose_payload,
        adjacent_page_context=_build_sample_adjacent_structured_context_for_dossier_v2(),
        compose_source_signature="compose-sig",
        source_sig_hash="sig-hash-v2",
    )


def _build_sample_experience_session_v2_narrative_brief(**overrides) -> dict:
    payload = {
        "focus_page": 7,
        "current_page_main_arc": "Current page compares concordance patterns and explains how the figure should anchor the reading order.",
        "continuity_resolutions": [
            "The previous page introduces the comparison so the current page can focus on the figure-driven contrast.",
            "The next page expands the table interpretation, which matters only insofar as it clarifies the current page.",
        ],
        "required_media_refs": [
            {"kind": "page_image", "page": 7, "ref": "https://example.com/p7.png"},
            {"kind": "figure", "page": 7, "layout_id": "layout:7:fig1"},
        ],
        "opening_key_points": [
            "本页先用 Fig 3 抓住答案-解释一致性与洞察密度这两个主指标。",
            "阅读时先看图里的总体比较，再回到正文解释 DOI 的意义。",
        ],
        "previous_page_bridge": {
            "page": 6,
            "key_points": ["上一页先把准确率和判定框架铺好。"],
            "bridge_text": "本页沿着这个结果框架，进一步问回答是否真的具有教学价值。",
        },
        "next_page_bridge": {
            "page": 8,
            "key_points": ["下一页会把 DOI 继续带入 discussion。"],
            "bridge_text": "读完本页的图证之后，下一页会把这些结果转成讨论层的解释。",
        },
        "content_strategy": "current_page_spine_with_inline_enrichment",
        "presentation_strategy": "renderer_bound_guided_reading",
        "meta": {
            "generator_mode": "model_generated_bootstrap",
            "build_mode": "phase2_model_narrative_brief",
        },
    }
    payload.update(overrides)
    return literature_api.ExperienceSessionV2NarrativeBrief.model_validate(payload).model_dump(mode="json")


def _build_sample_rich_experience_session_v2_narrative_brief(**overrides) -> dict:
    payload = {
        "focus_page": 8,
        "current_page_main_arc": {
            "section_type": "discussion_opening",
            "primary_claim": "Current page opens the discussion by tying accuracy and insight together.",
            "supporting_evidence_structure": [
                {
                    "claim_id": "claim-1",
                    "claim_text": "The page compares passing-threshold accuracy with the insight-density evidence.",
                }
            ],
        },
        "continuity_resolutions": {
            "from_previous_page": {
                "page_number": 7,
                "resolution_strategy": "surface_figure_context",
                "specific_resolutions": [
                    {
                        "reference_text": "Fig 3D",
                        "resolution_action": "reuse the previous page figure context when DOI is referenced",
                    }
                ],
            },
            "to_next_page": {
                "page_number": 9,
                "specific_resolutions": [
                    {
                        "transition_text": "Sentence fragment continues on the next page.",
                        "repair_note": "Preserve the open-ended continuation.",
                    }
                ],
            },
        },
        "required_media_refs": [
            {"kind": "page_image", "page": 8, "ref": "https://example.com/p8.png"},
            {"kind": "figure", "page": 7, "layout_id": "layout:7:fig3d"},
        ],
        "opening_key_points": [
            "这一页把结果页里的 DOI 与 discussion 的解释任务接起来。",
            "读者要先抓住上一页 Fig 3D 的 DOI 线索，再看 discussion 怎样接手。",
        ],
        "previous_page_bridge": {
            "page": 7,
            "key_points": ["上一页用 Fig 3 建立了 DOI 的比较结果。"],
            "bridge_text": "本页不再重做结果，而是把 DOI 结果转成讨论层的解释。",
        },
        "next_page_bridge": {
            "page": 9,
            "key_points": ["下一页会继续展开讨论句子的后半段。"],
            "bridge_text": "当前页页尾的开放句会在下一页完成，所以这里要保留向前延伸的感觉。",
        },
        "content_strategy": {
            "primary_focus": "Use the discussion header to pivot from results into interpretation.",
            "reading_order": [
                "Reconnect DOI from the previous page",
                "Read the discussion opening claim",
                "Keep the page-bottom continuation visible",
            ],
        },
        "presentation_strategy": {
            "layout_recommendation": "editorial spine with contextual figure support",
            "interaction_model": "surface prior figure context only when DOI claims are read",
        },
        "meta": {
            "generator_mode": "model_generated_bootstrap",
            "build_mode": "phase2_model_narrative_brief",
        },
    }
    payload.update(overrides)
    return literature_api.ExperienceSessionV2NarrativeBrief.model_validate(payload).model_dump(mode="json")


def _build_sample_experience_session_v2_artifact_draft(**overrides) -> dict:
    payload = {
        "focus_page": 7,
        "template_hint": "guided_mixed_media_v1",
        "layout_recipe": "current_page_spine_interleave_v1",
        "presentation_mode": "mixed_layout",
        "widget_family": "reader_v2_surface",
        "motion_preset": "calm_progressive",
        "interaction_policy": "reader_first_guided",
        "nodes": [
            {
                "node_kind": "heading",
                "text": "先用 Figure 3 确定这一页的比较对象。",
            },
            {
                "node_kind": "original_excerpt",
                "display_text": "We first examined the frequency of insight.",
                "source_layout_ids": ["layout:7:1"],
                "source_block_ids": ["blk-7-1"],
            },
            {
                "node_kind": "paragraph",
                "text": "这一页的主任务不是重复上一页，而是把图里的比较关系和正文里的 insight 频率重新对齐。",
            },
            {
                "node_kind": "figure_slot",
                "label": "Figure 3",
                "caption": "把图里的 concordance 比较作为当前页视觉锚点。",
                "source_layout_ids": ["layout:7:fig1"],
            },
            {
                "node_kind": "term_note",
                "term": "Insight",
                "definition": "这里的 insight 指 AI 给出非显而易见、但与临床推理相关的解释。",
            },
            {
                "node_kind": "external_resource",
                "label": "Paper URL",
                "resource_ref_ids": ["seed:1"],
            },
        ],
        "resource_requests": [],
        "meta": {
            "generator_mode": "model_generated_artifact_draft",
            "build_mode": "phase3_model_artifact_draft",
        },
    }
    payload.update(overrides)
    return literature_api.ExperienceSessionV2ArtifactDraft.model_validate(payload).model_dump(mode="json")


def _patch_fake_experience_session_v2_artifact_draft_generator(monkeypatch, **overrides):
    async def _fake_generate(*, reading_dossier, session_payload, resource_bundle, previous_draft=None, include_full_dossier):
        assert reading_dossier["version"] == "reading_dossier_v2"
        assert session_payload["version"] == "experience_session_v2"
        assert "bundle_entries" in resource_bundle
        assert include_full_dossier or previous_draft is not None
        return _build_sample_experience_session_v2_artifact_draft(**overrides)

    monkeypatch.setattr(literature_api, "_generate_experience_session_v2_artifact_draft", _fake_generate)


def _patch_fake_experience_session_v2_narrative_brief_generator(monkeypatch, **overrides):
    async def _fake_generate(*, reading_dossier, focus_page, reader_profile, user_intent):
        assert reading_dossier["version"] == "reading_dossier_v2"
        override_meta = dict(overrides.get("meta") or {})
        brief_overrides = {k: v for k, v in overrides.items() if k != "meta"}
        return _build_sample_experience_session_v2_narrative_brief(
            focus_page=int(focus_page),
            meta={
                "generator_mode": "model_generated_bootstrap",
                "build_mode": "phase2_model_narrative_brief",
                "reader_profile": str(reader_profile or "").strip(),
                "user_intent": str(user_intent or "").strip(),
                **override_meta,
            },
            **brief_overrides,
        )

    monkeypatch.setattr(literature_api, "_generate_experience_session_v2_narrative_brief", _fake_generate)


def _make_fake_v2_compose_service(compose_payload: dict):
    class _FakeComposeService:
        async def build_or_get_composed_payload(self, **_kwargs):
            payload = dict(compose_payload)
            meta = SimpleNamespace(
                cache_hit=True,
                cache_layer="redis",
                build_mode=str(payload.get("build_mode") or ""),
                source_signature=str(payload.get("source_signature") or ""),
                source_sig_hash="sig-hash-v2",
            )
            return payload, meta

    return _FakeComposeService()


def _build_sample_page_artifact_v2_authored_plan() -> dict:
    return {
        "template_id": "reader_focus_v1",
        "layout_recipe": "excerpt_explain_interleave_v1",
        "presentation_mode": "guided_reading",
        "widget_family": "reader_widgets_v1",
        "motion_preset": "subtle_progressive",
        "interaction_policy": "inline_context_actions",
        "authored_explanations": [
            "This excerpt establishes the page's central observation.",
            "The authored layer clarifies how the evidence should be interpreted.",
        ],
        "excerpt_overrides": [
            {
                "display_text": "We first examined the frequency of insight.",
                "source_layout_ids": ["layout:7:1"],
                "source_block_ids": ["blk-7-1"],
            }
        ],
        "figure_slots": [
            {
                "label": "Figure 3",
                "caption": "USMLE insight concordance panel",
                "source_layout_id": "layout:7:fig1",
            }
        ],
        "term_annotations": [
            {
                "term": "insight frequency",
                "definition": "How often model outputs include novel, valid takeaways.",
            }
        ],
        "external_resources": [
            {
                "label": "Evaluation rubric",
                "url": "https://example.com/rubric",
                "resource_type": "reference",
            }
        ],
    }


def _build_extended_page_artifact_v2_authored_plan() -> dict:
    plan = _build_sample_page_artifact_v2_authored_plan()
    plan.update(
        {
            "table_slots": [
                {"label": "Table 1", "caption": "Score breakdown table", "source_layout_id": "layout:7:tbl1"}
            ],
            "equation_slots": [
                {"label": "(1)", "caption": "Score composition equation", "source_layout_id": "layout:7:eq1"}
            ],
            "aside_blocks": [
                {"label": "Why this matters", "text": "This aside keeps the guided-reading narrative flexible."}
            ],
            "requested_node_kinds": ["table_slot", "equation_slot", "aside_content"],
        }
    )
    return plan


def _build_structured_page_artifact_v2_authored_plan() -> dict:
    plan = _build_sample_page_artifact_v2_authored_plan()
    plan.update(
        {
            "authored_text_blocks": [
                {"segment_kind": "heading", "text": "先用图把这一页的主问题钉住。"},
                {"segment_kind": "paragraph", "text": "这一段解释为什么图里的比较是当前页最值得先看的证据。"},
            ],
            "authored_explanations": [],
        }
    )
    return plan


def test_page_artifact_v2_should_preserve_required_presentation_fields_after_validation():
    dossier = _build_sample_reading_dossier_v2_for_session()
    authored_plan = _build_sample_page_artifact_v2_authored_plan()

    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=dossier,
        authored_plan=authored_plan,
        session_id="sess-phase3-1",
    )
    report = literature_api._validate_page_artifact_v2_contract(artifact)

    assert artifact["template_id"] == authored_plan["template_id"]
    assert artifact["layout_recipe"] == authored_plan["layout_recipe"]
    assert artifact["presentation_mode"] == authored_plan["presentation_mode"]
    assert artifact["widget_family"] == authored_plan["widget_family"]
    assert artifact["motion_preset"] == authored_plan["motion_preset"]
    assert artifact["interaction_policy"] == authored_plan["interaction_policy"]
    assert report["valid"] is True
    assert report["renderable"] is True


def test_page_artifact_v2_should_interleave_original_excerpts_and_authored_explanations():
    dossier = _build_sample_reading_dossier_v2_for_session()
    authored_plan = _build_sample_page_artifact_v2_authored_plan()

    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=dossier,
        authored_plan=authored_plan,
    )
    kinds = [block["segment_kind"] for block in artifact["reading_blocks"]]

    assert "original_excerpt" in kinds
    assert "authored_explanation" in kinds
    assert "figure_slot" in kinds
    assert "term_annotation" in kinds
    assert "external_resource" in kinds
    assert kinds[0] == "original_excerpt"
    assert kinds[1] == "authored_explanation"
    assert kinds.count("original_excerpt") == 1


def test_page_artifact_v2_should_allow_missing_external_resources_when_page_does_not_need_them():
    dossier = _build_sample_reading_dossier_v2_for_session()
    authored_plan = _build_sample_page_artifact_v2_authored_plan()
    authored_plan["external_resources"] = []

    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=dossier,
        authored_plan=authored_plan,
    )
    report = literature_api._validate_page_artifact_v2_contract(artifact)

    assert report["valid"] is True
    assert report["renderable"] is True
    assert "external_resource" not in [block["segment_kind"] for block in artifact["reading_blocks"]]


def test_page_artifact_v2_should_preserve_structured_heading_and_paragraph_blocks():
    dossier = _build_sample_reading_dossier_v2_for_session()
    authored_plan = _build_structured_page_artifact_v2_authored_plan()

    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=dossier,
        authored_plan=authored_plan,
    )

    kinds = [block["segment_kind"] for block in artifact["reading_blocks"]]
    assert "heading" in kinds
    assert "paragraph" in kinds
    assert "authored_explanation" not in kinds


def test_page_artifact_v2_should_allow_draft_selected_excerpt_subset_without_near_complete_coverage():
    dossier = _build_sample_reading_dossier_v2_for_session()
    dossier["current_page"]["rich_grounding"]["reading_nodes"].append(
        {
            "node_id": "paragraph_2",
            "node_kind": "paragraph",
            "clean_text": "A second current-page paragraph remains available in grounding but is not selected by the draft.",
            "source_layout_ids": ["layout:7:2"],
            "source_block_ids": ["blk-7-2"],
            "include_in_main_flow": True,
        }
    )
    authored_plan = _build_sample_page_artifact_v2_authored_plan()

    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=dossier,
        authored_plan=authored_plan,
    )
    report = literature_api._validate_page_artifact_v2_contract(artifact)

    excerpt_blocks = [block for block in artifact["reading_blocks"] if block["segment_kind"] == "original_excerpt"]
    assert report["valid"] is True
    assert report["renderable"] is True
    assert len(excerpt_blocks) == 1
    assert excerpt_blocks[0]["source_layout_ids"] == ["layout:7:1"]
    assert artifact["current_page_spine"]["meta"]["coverage_mode"] == "draft_selected_excerpt_rows"
    assert artifact["current_page_spine"]["meta"]["candidate_excerpt_count"] >= 2
    assert artifact["current_page_spine"]["meta"]["selected_excerpt_count"] == 1


def test_page_artifact_v2_should_resolve_multiple_draft_selected_excerpt_slices_from_same_grounding_row():
    dossier = _build_sample_reading_dossier_v2_for_session()
    dossier["current_page"]["rich_grounding"]["reading_nodes"][0]["clean_text"] = (
        "We first examined the frequency of insight. "
        "Overall, ChatGPT produced at least one significant insight in 88.9% of all responses. "
        "Insight frequency was generally consistent between exam type and question input format."
    )
    dossier["current_page"]["rich_grounding"]["reading_nodes"][0]["source_block_ids"] = [
        "blk-7-1",
        "blk-7-2",
        "blk-7-3",
    ]
    dossier["current_page"]["rich_grounding"]["evidence_map"][0]["source_block_ids"] = [
        "blk-7-1",
        "blk-7-2",
        "blk-7-3",
    ]

    authored_plan = _build_sample_page_artifact_v2_authored_plan()
    authored_plan["excerpt_overrides"] = [
        {
            "display_text": "We first examined the frequency of insight.",
            "source_layout_ids": ["layout:7:1"],
            "source_block_ids": ["blk-7-1"],
            "meta": {"group_id": "g1", "placement": "inline"},
        },
        {
            "display_text": "Overall, ChatGPT produced at least one significant insight in 88.9% of all responses.",
            "source_layout_ids": ["layout:7:1"],
            "source_block_ids": ["blk-7-2"],
            "meta": {"group_id": "g1", "placement": "inline"},
        },
    ]
    authored_plan.setdefault("meta", {})
    authored_plan["meta"]["draft_node_sequence"] = [
        {
            "node_kind": "original_excerpt",
            "display_text": "We first examined the frequency of insight.",
            "source_layout_ids": ["layout:7:1"],
            "source_block_ids": ["blk-7-1"],
            "meta": {"group_id": "g1", "placement": "inline"},
        },
        {
            "node_kind": "paragraph",
            "text": "先用第一句建立 insight 频率这个问题。",
            "meta": {"group_id": "g1", "placement": "block"},
        },
        {
            "node_kind": "original_excerpt",
            "display_text": "Overall, ChatGPT produced at least one significant insight in 88.9% of all responses.",
            "source_layout_ids": ["layout:7:1"],
            "source_block_ids": ["blk-7-2"],
            "meta": {"group_id": "g1", "placement": "inline"},
        },
        {
            "node_kind": "paragraph",
            "text": "再把 88.9% 这个核心结果拉出来解释。",
            "meta": {"group_id": "g1", "placement": "block"},
        },
    ]

    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=dossier,
        authored_plan=authored_plan,
    )

    original_blocks = [
        block for block in artifact["reading_blocks"]
        if block["segment_kind"] == "original_excerpt"
    ]
    assert len(original_blocks) == 2
    assert original_blocks[0]["source_block_ids"] == ["blk-7-1"]
    assert original_blocks[1]["source_block_ids"] == ["blk-7-2"]


def test_page_artifact_v2_should_resolve_excerpt_from_direct_grounding_when_candidate_rows_are_too_coarse():
    dossier = _build_sample_reading_dossier_v2_for_session()
    dossier["current_page"]["rich_grounding"]["reading_nodes"][0]["clean_text"] = (
        "We first examined the frequency of insight. "
        "Overall, ChatGPT produced at least one significant insight in 88.9% of all responses. "
        "Insight frequency was generally consistent between exam type and question input format."
    )
    dossier["current_page"]["rich_grounding"]["reading_nodes"][0]["source_block_ids"] = [
        "blk-7-1",
        "blk-7-2",
        "blk-7-3",
        "blk-7-4",
        "blk-7-5",
        "blk-7-6",
        "blk-7-7",
    ]
    authored_plan = _build_sample_page_artifact_v2_authored_plan()
    authored_plan["excerpt_overrides"] = [
        {
            "display_text": "Insight frequency was generally consistent between exam type and question input format.",
            "source_layout_ids": ["layout:7:1"],
            "source_block_ids": ["blk-7-7"],
            "meta": {"group_id": "g2", "placement": "inline"},
        }
    ]
    authored_plan.setdefault("meta", {})
    authored_plan["meta"]["draft_node_sequence"] = [
        {
            "node_kind": "original_excerpt",
            "display_text": "Insight frequency was generally consistent between exam type and question input format.",
            "source_layout_ids": ["layout:7:1"],
            "source_block_ids": ["blk-7-7"],
            "meta": {"group_id": "g2", "placement": "inline"},
        },
        {
            "node_kind": "paragraph",
            "text": "这里强调 insight frequency 在不同输入条件下保持一致。",
            "meta": {"group_id": "g2", "placement": "block"},
        },
    ]

    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=dossier,
        authored_plan=authored_plan,
    )

    original_blocks = [block for block in artifact["reading_blocks"] if block["segment_kind"] == "original_excerpt"]
    assert len(original_blocks) == 1
    assert original_blocks[0]["source_layout_ids"] == ["layout:7:1"]
    assert original_blocks[0]["source_block_ids"] == ["blk-7-7"]


def test_page_artifact_v2_should_resolve_media_caption_excerpt_even_if_not_preselected_as_primary_candidate():
    dossier = _build_sample_reading_dossier_v2_for_session()
    dossier["current_page"]["rich_grounding"]["layout_atoms"].append(
        {
            "layout_id": "layout:7:fig2",
            "layout_type": "figure",
            "clean_text": "Fig 3. Concordance and insight density metrics for educational quality.",
            "canonical_block_ids": ["blk-7-fig2-1", "blk-7-fig2-2"],
            "include_in_main_flow": True,
        }
    )
    authored_plan = _build_sample_page_artifact_v2_authored_plan()
    authored_plan["excerpt_overrides"] = [
        {
            "display_text": "Fig 3. Concordance and insight density metrics for educational quality.",
            "source_layout_ids": ["layout:7:fig2"],
            "source_block_ids": ["blk-7-fig2-1", "blk-7-fig2-2"],
            "meta": {"group_id": "g-fig", "placement": "inline"},
        }
    ]
    authored_plan.setdefault("meta", {})
    authored_plan["meta"]["draft_node_sequence"] = [
        {
            "node_kind": "original_excerpt",
            "display_text": "Fig 3. Concordance and insight density metrics for educational quality.",
            "source_layout_ids": ["layout:7:fig2"],
            "source_block_ids": ["blk-7-fig2-1", "blk-7-fig2-2"],
            "meta": {"group_id": "g-fig", "placement": "inline"},
        },
        {
            "node_kind": "paragraph",
            "text": "这张图把 concordance 和 DOI 两个指标并排拉到主线上。",
            "meta": {"group_id": "g-fig", "placement": "block"},
        },
    ]

    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=dossier,
        authored_plan=authored_plan,
    )

    original_blocks = [block for block in artifact["reading_blocks"] if block["segment_kind"] == "original_excerpt"]
    assert len(original_blocks) == 1
    assert original_blocks[0]["source_layout_ids"] == ["layout:7:fig2"]


def test_page_artifact_v2_should_preserve_full_reader_facing_excerpt_text_without_truncation():
    dossier = _build_sample_reading_dossier_v2_for_session()
    long_caption = (
        "Fig 3. Concordance and insight of ChatGPT on USMLE. For USMLE Steps 1, 2CK, and 3, AI outputs were "
        "adjudicated on concordance and density of insight based on the ACI scoring system. Panel A summarizes "
        "overall concordance across exam types and encoding formats. Panel B stratifies concordance between accurate "
        "and inaccurate outputs. Panel C reports whether at least one significant insight appears. Panel D compares "
        "density of insight scores across cohorts and highlights where educational value rises above simple correctness."
    )
    dossier["current_page"]["rich_grounding"]["layout_atoms"].append(
        {
            "layout_id": "layout:7:fig3-caption-long",
            "layout_type": "figure_name",
            "clean_text": long_caption,
            "canonical_block_ids": ["blk-7-fig3-cap-1", "blk-7-fig3-cap-2", "blk-7-fig3-cap-3"],
            "include_in_main_flow": True,
        }
    )
    authored_plan = _build_sample_page_artifact_v2_authored_plan()
    authored_plan["excerpt_overrides"] = [
        {
            "display_text": long_caption,
            "source_layout_ids": ["layout:7:fig3-caption-long"],
            "source_block_ids": ["blk-7-fig3-cap-1", "blk-7-fig3-cap-2", "blk-7-fig3-cap-3"],
            "meta": {"group_id": "g-fig3", "placement": "inline"},
        }
    ]
    authored_plan.setdefault("meta", {})
    authored_plan["meta"]["draft_node_sequence"] = [
        {
            "node_kind": "original_excerpt",
            "display_text": long_caption,
            "source_layout_ids": ["layout:7:fig3-caption-long"],
            "source_block_ids": ["blk-7-fig3-cap-1", "blk-7-fig3-cap-2", "blk-7-fig3-cap-3"],
            "meta": {"group_id": "g-fig3", "placement": "inline"},
        },
        {
            "node_kind": "paragraph",
            "text": "这里保留完整图注，不再在最终 reader artifact 里用省略号截断。",
            "meta": {"group_id": "g-fig3", "placement": "block"},
        },
    ]

    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=dossier,
        authored_plan=authored_plan,
    )

    excerpt_blocks = [block for block in artifact["reading_blocks"] if block["segment_kind"] == "original_excerpt"]
    assert len(excerpt_blocks) == 1
    assert excerpt_blocks[0]["text"] == long_caption
    assert not excerpt_blocks[0]["text"].endswith("…")


def test_page_artifact_v2_should_not_auto_insert_default_term_or_figure_blocks():
    dossier = _build_sample_reading_dossier_v2_for_session()
    authored_plan = _build_sample_page_artifact_v2_authored_plan()
    authored_plan["figure_slots"] = []
    authored_plan["term_annotations"] = []
    authored_plan["external_resources"] = []

    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=dossier,
        authored_plan=authored_plan,
    )
    report = literature_api._validate_page_artifact_v2_contract(artifact)

    kinds = [block["segment_kind"] for block in artifact["reading_blocks"]]
    assert report["valid"] is True
    assert report["renderable"] is True
    assert "figure_slot" not in kinds
    assert "term_annotation" not in kinds


def test_page_artifact_v2_should_prefer_clean_excerpt_and_skip_ocr_heavy_figure_text():
    dossier = _build_sample_reading_dossier_v2_for_session()
    long_prose = " ".join(["insight"] * 180)
    dossier["current_page"]["rich_grounding"]["reading_nodes"][0]["clean_text"] = (
        "We first examined the frequency of insight. " + long_prose
    )
    dossier["current_page"]["rich_grounding"]["reading_nodes"].append(
        {
            "node_id": "node:7:fig-heavy",
            "node_kind": "figure",
            "clean_text": "Figure OCR " + ("A1 | B2 | C3 | " * 120),
            "source_layout_ids": ["layout:7:fig1"],
            "source_block_ids": ["blk-7-fig1"],
            "include_in_main_flow": True,
        }
    )

    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=dossier,
        authored_plan=_build_sample_page_artifact_v2_authored_plan(),
    )

    original_blocks = [
        block for block in artifact["reading_blocks"]
        if block["segment_kind"] == "original_excerpt"
    ]

    assert original_blocks
    assert all("Figure OCR" not in str(block.get("text") or "") for block in original_blocks)
    assert all(len(str(block.get("text") or "")) <= 361 for block in original_blocks)
    assert set(artifact["current_page_spine"]["layout_ids"]) == {"layout:7:1"}


def test_page_artifact_v2_should_bind_figure_slots_to_current_page_grounding():
    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=_build_sample_reading_dossier_v2_for_session(),
        authored_plan=_build_sample_page_artifact_v2_authored_plan(),
    )
    figure_blocks = [
        block for block in artifact["reading_blocks"]
        if block["segment_kind"] == "figure_slot"
    ]

    assert figure_blocks
    block = figure_blocks[0]
    assert block["source_layout_ids"] == ["layout:7:fig1"]
    assert block["evidence_ids"] == ["ev-7-fig1"]
    assert block["meta"]["binding_kind"] == "figure_layout_anchor"
    assert block["meta"]["page_image_url"] == "https://example.com/p7.png"
    assert block["meta"]["media_binding"]["page_asset_ref"] == "http://localhost:8888/api/v1/literature/reader/figure-assets/78/7/layout_7_fig1"


def test_page_artifact_v2_should_bind_real_figure_slots_to_current_page_figure_anchors():
    dossier = json.loads(
        Path("docs/plan/fixtures/reading_dossier_v2_control_sample_p78_p7.json").read_text(encoding="utf-8")
    )
    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=dossier,
        authored_plan={
            **_build_sample_page_artifact_v2_authored_plan(),
            "excerpt_overrides": [
                {
                    "display_text": "We first examined the frequency (prevalence) of insight.",
                    "source_layout_ids": ["b7f30cb723aa8f9eb203713b91ae19a3"],
                    "source_block_ids": ["p7_dm_p7_l010_b001"],
                }
            ],
            "figure_slots": [{"label": "Figure 3", "figure_ref": "fig:p7:1"}],
        },
        session_id="sess-phase3-real-figure",
    )

    figure_blocks = [
        block for block in artifact["reading_blocks"]
        if block["segment_kind"] == "figure_slot"
    ]
    assert figure_blocks
    block = figure_blocks[0]
    assert block["source_layout_ids"] == ["05fb9340aa7b7a3ad2bdd0643c63d6a3"]
    assert block["evidence_ids"] == ["layout:05fb9340aa7b7a3ad2bdd0643c63d6a3"]
    assert block["meta"]["binding_kind"] == "figure_layout_anchor"
    assert block["meta"]["page_image_url"] == "http://localhost:8888/api/v1/literature/reader/grounding-page-assets/78/7"
    assert block["meta"]["media_binding"]["page_asset_ref"] == "http://localhost:8888/api/v1/literature/reader/figure-assets/78/7/05fb9340aa7b7a3ad2bdd0643c63d6a3"


def test_page_artifact_v2_should_bind_media_slot_figure_to_concrete_asset_ref():
    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=_build_sample_reading_dossier_v2_for_session(),
        authored_plan={
            **_build_sample_page_artifact_v2_authored_plan(),
            "media_slots": [
                {
                    "label": "Figure media",
                    "media_type": "figure",
                    "source_layout_id": "layout:7:fig1",
                }
            ],
            "requested_node_kinds": ["media_slot"],
        },
    )
    media_blocks = [
        block for block in artifact["reading_blocks"]
        if block["segment_kind"] == "media_slot"
    ]
    assert media_blocks
    block = media_blocks[0]
    assert block["source_layout_ids"] == ["layout:7:fig1"]
    assert block["meta"]["binding_kind"] == "media_layout_anchor"
    assert block["meta"]["media_binding"]["page_asset_ref"] == "http://localhost:8888/api/v1/literature/reader/figure-assets/78/7/layout_7_fig1"


def test_page_artifact_v2_should_keep_current_page_spine_primary_and_adjacent_as_latent_context():
    compose_payload = _build_sample_compose_payload_for_dossier_v2()
    dossier = _invoke_reading_dossier_v2_builder(
        focus_page=7,
        reader_profile="curious_generalist",
        compose_payload=compose_payload,
        adjacent_page_context=_build_sample_adjacent_structured_context_for_dossier_v2(),
        compose_source_signature="compose-sig",
        source_sig_hash="sig-hash-v2",
    )
    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=dossier,
        authored_plan=_build_sample_page_artifact_v2_authored_plan(),
    )
    original_blocks = [
        block for block in artifact["reading_blocks"]
        if block["segment_kind"] == "original_excerpt"
    ]
    original_ids = {block["segment_id"] for block in original_blocks}
    spine_ids = set(artifact["current_page_spine"]["main_segment_ids"])

    assert artifact["current_page_spine"]["primary"] is True
    assert all(block["source_lane"] == "current_page" for block in original_blocks)
    assert spine_ids
    assert spine_ids.issubset(original_ids)
    assert artifact["provenance"]["continuity_mode"] == "current_page_primary_ordered_adjacent_context"
    assert artifact["provenance"]["include_adjacent_as_coequal_anchor"] is False
    assert artifact["provenance"]["adjacent_context_pages"] == [6, 8]
    assert artifact["provenance"]["source_lanes"]["adjacent_pages_meta"]["fidelity"] == "ordered_structured_context"


def test_page_artifact_v2_should_preserve_all_main_flow_excerpt_anchors_in_spine():
    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=_build_sample_reading_dossier_v2_for_session(),
        authored_plan=_build_sample_page_artifact_v2_authored_plan(),
    )

    original_blocks = [
        block for block in artifact["reading_blocks"]
        if block["segment_kind"] == "original_excerpt"
    ]
    expected_layout_ids = set(artifact["current_page_spine"]["layout_ids"])
    represented_layout_ids = {
        layout_id
        for block in original_blocks
        for layout_id in block.get("source_layout_ids", [])
    }

    assert expected_layout_ids == {"layout:7:1"}
    assert represented_layout_ids == expected_layout_ids
    assert artifact["current_page_spine"]["meta"]["coverage_mode"] == "draft_selected_excerpt_rows"
    assert artifact["current_page_spine"]["meta"]["selected_excerpt_count"] == 1
    assert artifact["current_page_spine"]["meta"]["candidate_excerpt_count"] >= 1
    assert artifact["current_page_spine"]["meta"]["represented_excerpt_count"] == 1
    assert artifact["current_page_spine"]["meta"]["coverage_ratio"] > 0


def test_page_artifact_v2_should_resolve_draft_selected_excerpts_by_text_when_ids_drift():
    dossier = _build_sample_reading_dossier_v2_for_session()
    authored_plan = _build_sample_page_artifact_v2_authored_plan()
    authored_plan["meta"] = {
        **authored_plan.get("meta", {}),
        "draft_node_sequence": [
            {
                "node_kind": "paragraph",
                "text": "先看当前页的结果段。",
                "meta": {"group_id": "g-results"},
            },
            {
                "node_kind": "original_excerpt",
                "display_text": "We first examined the frequency of insight.",
                "source_layout_ids": ["layout:7:missing"],
                "source_block_ids": ["blk-7-missing"],
                "meta": {"group_id": "g-results", "placement": "inline"},
            },
        ],
    }

    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=dossier,
        authored_plan=authored_plan,
    )

    excerpt_blocks = [block for block in artifact["reading_blocks"] if block["segment_kind"] == "original_excerpt"]
    assert len(excerpt_blocks) == 1
    assert excerpt_blocks[0]["source_layout_ids"] == ["layout:7:1"]
    assert excerpt_blocks[0]["meta"]["placement"] == "inline"


def test_page_artifact_v2_should_support_table_equation_and_aside_blocks_incrementally():
    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=_build_sample_reading_dossier_v2_for_session(),
        authored_plan=_build_extended_page_artifact_v2_authored_plan(),
    )
    kinds = [block["segment_kind"] for block in artifact["reading_blocks"]]

    assert "table_slot" in kinds
    assert "equation_slot" in kinds
    assert "aside_content" in kinds
    report = literature_api._validate_page_artifact_v2_contract(artifact)
    assert report["valid"] is True
    assert report["renderable"] is True


def test_page_artifact_v2_should_fail_loudly_for_unsupported_requested_node_kind():
    with pytest.raises(ValueError, match="requested artifact node kind not supported yet: unsupported_widget"):
        literature_api._build_page_artifact_v2_from_dossier(
            reading_dossier=_build_sample_reading_dossier_v2_for_session(),
            authored_plan={
                **_build_sample_page_artifact_v2_authored_plan(),
                "requested_node_kinds": ["unsupported_widget"],
            },
        )


def test_page_artifact_v2_should_fail_loudly_for_unresolved_table_binding():
    with pytest.raises(ValueError, match="media slot binding could not be resolved: table_slot"):
        literature_api._build_page_artifact_v2_from_dossier(
            reading_dossier=_build_sample_reading_dossier_v2_for_session(),
            authored_plan={
                **_build_sample_page_artifact_v2_authored_plan(),
                "table_slots": [{"label": "Table 1", "source_layout_id": "layout:7:missing"}],
                "requested_node_kinds": ["table_slot"],
            },
        )


def test_page_artifact_v2_should_fail_loudly_when_figure_layout_anchor_has_no_concrete_asset_ref():
    dossier = _build_sample_reading_dossier_v2_for_session()
    dossier["current_page"]["build_meta"]["compose_assets"] = []
    with pytest.raises(ValueError, match="media slot binding concrete asset ref missing: figure_slot"):
        literature_api._build_page_artifact_v2_from_dossier(
            reading_dossier=dossier,
            authored_plan=_build_sample_page_artifact_v2_authored_plan(),
        )


def test_page_artifact_v2_should_reject_missing_presentation_or_missing_main_spine():
    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=_build_sample_reading_dossier_v2_for_session(),
        authored_plan=_build_sample_page_artifact_v2_authored_plan(),
    )

    missing_presentation = json.loads(json.dumps(artifact, ensure_ascii=False))
    missing_presentation.pop("template_id", None)
    missing_presentation_report = literature_api._validate_page_artifact_v2_contract(missing_presentation)
    assert missing_presentation_report["valid"] is False

    missing_spine = json.loads(json.dumps(artifact, ensure_ascii=False))
    missing_spine["current_page_spine"] = {
        "page": artifact["focus_page"],
        "owner": "reading_dossier_v2.current_page",
        "primary": True,
        "reading_node_ids": [],
        "layout_ids": [],
        "block_ids": [],
        "evidence_ids": [],
        "main_segment_ids": [],
        "meta": {},
    }
    missing_spine_report = literature_api._validate_page_artifact_v2_contract(missing_spine)
    assert missing_spine_report["valid"] is False


def test_page_artifact_v2_should_allow_missing_figure_slot_when_draft_did_not_request_it():
    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=_build_sample_reading_dossier_v2_for_session(),
        authored_plan=_build_sample_page_artifact_v2_authored_plan(),
    )
    without_figure_slot = json.loads(json.dumps(artifact, ensure_ascii=False))
    without_figure_slot["reading_blocks"] = [
        item for item in without_figure_slot["reading_blocks"]
        if item["segment_kind"] != "figure_slot"
    ]

    report = literature_api._validate_page_artifact_v2_contract(without_figure_slot)
    assert report["valid"] is True
    assert report["renderable"] is True


def test_page_artifact_v2_should_reject_unbound_figure_slot_or_incomplete_spine_coverage():
    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=_build_sample_reading_dossier_v2_for_session(),
        authored_plan=_build_sample_page_artifact_v2_authored_plan(),
    )

    unbound_figure = json.loads(json.dumps(artifact, ensure_ascii=False))
    for block in unbound_figure["reading_blocks"]:
        if block["segment_kind"] == "figure_slot":
            block["source_layout_ids"] = []
            block["evidence_ids"] = []
            block["meta"]["binding_kind"] = ""
            break
    unbound_report = literature_api._validate_page_artifact_v2_contract(unbound_figure)
    assert unbound_report["valid"] is False
    assert any("figure_slot" in str(err) for err in unbound_report["errors"])

    real_dossier = json.loads(
        Path("docs/plan/fixtures/reading_dossier_v2_control_sample_p78_p7.json").read_text(encoding="utf-8")
    )
    real_artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=real_dossier,
        authored_plan={
            **_build_sample_page_artifact_v2_authored_plan(),
            "excerpt_overrides": [
                {
                    "display_text": "We first examined the frequency (prevalence) of insight.",
                    "source_layout_ids": ["b7f30cb723aa8f9eb203713b91ae19a3"],
                    "source_block_ids": ["p7_dm_p7_l010_b001"],
                }
            ],
            "figure_slots": [{"label": "Figure 3", "figure_ref": "fig:p7:1"}],
        },
    )

    weak_spine = json.loads(json.dumps(real_artifact, ensure_ascii=False))
    removed_segment_id = weak_spine["current_page_spine"]["main_segment_ids"][0]
    weak_spine["reading_blocks"] = [
        block for block in weak_spine["reading_blocks"]
        if block["segment_id"] != removed_segment_id
    ]
    weak_spine["current_page_spine"]["main_segment_ids"] = [
        segment_id for segment_id in weak_spine["current_page_spine"]["main_segment_ids"]
        if segment_id != removed_segment_id
    ]
    weak_spine["current_page_spine"]["meta"]["represented_excerpt_count"] = 0
    weak_spine["current_page_spine"]["meta"]["coverage_ratio"] = 0.0
    weak_spine["current_page_spine"]["meta"]["excerpt_coverage"]["covered_main_flow_count"] = 0
    weak_spine["current_page_spine"]["meta"]["excerpt_coverage"]["coverage_ratio"] = 0.0
    weak_spine_report = literature_api._validate_page_artifact_v2_contract(weak_spine)
    assert weak_spine_report["valid"] is False
    assert any(
        "original excerpt" in str(err).lower()
        or "main_segment_ids" in str(err).lower()
        or "layout anchors" in str(err).lower()
        for err in weak_spine_report["errors"]
    )


def test_page_artifact_v2_renderability_validator_should_pass_valid_artifact():
    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=_build_sample_reading_dossier_v2_for_session(),
        authored_plan=_build_sample_page_artifact_v2_authored_plan(),
    )

    assert literature_api._is_page_artifact_v2_renderable(artifact) is True


def test_experience_session_v2_context_should_default_instantiate_without_validation_error():
    context = literature_api.ExperienceSessionV2ContextCarry()

    assert context.mode == "full_dossier_bootstrap"
    assert context.full_dossier is not None


def test_experience_session_v2_iteration_should_require_narrative_brief_for_bootstrap():
    with pytest.raises(ValueError, match="bootstrap iteration requires narrative_brief"):
        literature_api.ExperienceSessionV2Iteration()


@pytest.mark.asyncio
async def test_experience_session_v2_bootstrap_should_generate_model_backed_structured_narrative_brief(monkeypatch):
    dossier = _build_sample_reading_dossier_v2_for_session()
    captured: dict[str, object] = {}

    async def _fake_call(**kwargs):
        captured["user_prompt_payload"] = kwargs["user_prompt_payload"]
        return _build_sample_experience_session_v2_narrative_brief(
            meta={"generator_mode": "model_generated_bootstrap", "build_mode": "phase2_model_narrative_brief"}
        )

    monkeypatch.setattr(literature_api, "_call_experience_session_v2_narrative_brief_model", _fake_call)

    brief = await literature_api._generate_experience_session_v2_narrative_brief(
        reading_dossier=dossier,
        focus_page=7,
        reader_profile="curious_generalist",
        user_intent="follow the figure first",
    )

    prompt_payload = captured["user_prompt_payload"]
    assert prompt_payload["reading_dossier_v2"]["version"] == "reading_dossier_v2"
    assert prompt_payload["rules"]["current_page_is_primary_narrative_anchor"] is True
    assert brief["meta"]["generator_mode"] == "model_generated_bootstrap"
    assert brief["content_strategy"] == "current_page_spine_with_inline_enrichment"


@pytest.mark.asyncio
async def test_parse_json_dict_from_model_text_should_extract_fenced_json_object():
    parsed = await literature_api.parse_json_dict_from_model_text(
        'Here is the JSON you requested:\\n```json\\n{"focus_page":7,"current_page_main_arc":"先看图。"}\\n```\\nDone.'
    )

    assert parsed["focus_page"] == 7
    assert parsed["current_page_main_arc"] == "先看图。"


@pytest.mark.asyncio
async def test_experience_session_v2_bootstrap_should_retry_once_for_invalid_json_output(monkeypatch):
    dossier = _build_sample_reading_dossier_v2_for_session()
    calls: list[str] = []

    async def _fake_call(**kwargs):
        calls.append(str(kwargs["system_prompt"]))
        if len(calls) == 1:
            raise ValueError("narrative brief generation failed: invalid JSON output")
        return _build_sample_experience_session_v2_narrative_brief()

    monkeypatch.setattr(literature_api, "_call_experience_session_v2_narrative_brief_model", _fake_call)

    brief = await literature_api._generate_experience_session_v2_narrative_brief(
        reading_dossier=dossier,
        focus_page=7,
        reader_profile="curious_generalist",
        user_intent="follow the figure first",
    )

    assert len(calls) == 2
    assert "Final reminder: return exactly one JSON object" in calls[1]
    assert brief["focus_page"] == 7


def test_experience_session_v2_narrative_brief_schema_should_accept_richer_strategy_objects():
    brief = _build_sample_rich_experience_session_v2_narrative_brief()

    assert brief["current_page_main_arc"]["primary_claim"]
    assert brief["continuity_resolutions"]["from_previous_page"]["resolution_strategy"] == "surface_figure_context"
    assert brief["content_strategy"]["reading_order"][0] == "Reconnect DOI from the previous page"
    assert brief["presentation_strategy"]["layout_recommendation"] == "editorial spine with contextual figure support"


@pytest.mark.asyncio
async def test_experience_session_v2_bootstrap_should_consume_full_adjacent_structured_context_not_preview(monkeypatch):
    dossier = _build_sample_reading_dossier_v2_for_session()
    dossier["adjacent_pages"]["pages"][0]["content_stream"].append(
        {
            "seq": 4,
            "type": "paragraph",
            "text": "Unique later continuity sentinel from the previous page.",
            "ocr_text": "Unique later continuity sentinel from the previous page.",
            "role": "body",
        }
    )
    captured: dict[str, object] = {}

    async def _fake_call(**kwargs):
        captured["user_prompt_payload"] = kwargs["user_prompt_payload"]
        return _build_sample_experience_session_v2_narrative_brief()

    monkeypatch.setattr(literature_api, "_call_experience_session_v2_narrative_brief_model", _fake_call)

    await literature_api._generate_experience_session_v2_narrative_brief(
        reading_dossier=dossier,
        focus_page=7,
        reader_profile="curious_generalist",
        user_intent="",
    )

    adjacent_stream = captured["user_prompt_payload"]["reading_dossier_v2"]["adjacent_pages"]["pages"][0]["content_stream"]
    assert len(adjacent_stream) == 4
    assert adjacent_stream[-1]["text"] == "Unique later continuity sentinel from the previous page."


def test_experience_session_v2_should_compact_richer_narrative_brief_for_revise_carry():
    dossier = _build_sample_reading_dossier_v2_for_session()
    session = literature_api._build_experience_session_v2(
        cache_key="lit:experience_session:v2:test",
        reading_dossier=dossier,
        focus_page=8,
        reader_profile="curious_generalist",
        max_iterations=4,
        max_tool_rounds=6,
        narrative_brief=_build_sample_rich_experience_session_v2_narrative_brief(),
    )

    updated = literature_api._append_experience_session_v2_iteration(
        session,
        phase="revise",
        delta_packet={"updated_sections": ["s1"]},
        state_handle="sess:iter:2",
    )

    compact_brief = updated["iterations"][1]["context_carry"]["delta_packet"]["narrative_brief"]
    assert isinstance(compact_brief["current_page_main_arc"], str)
    assert "accuracy and insight together" in compact_brief["current_page_main_arc"]
    assert isinstance(compact_brief["continuity_resolutions"], list)
    assert compact_brief["continuity_resolutions"]
    assert isinstance(compact_brief["content_strategy"], str)
    assert "discussion header" in compact_brief["content_strategy"]
    assert isinstance(compact_brief["presentation_strategy"], str)


@pytest.mark.asyncio
async def test_experience_session_v2_bootstrap_should_fail_loudly_for_invalid_model_generated_brief(monkeypatch):
    async def _fake_call(**kwargs):
        return {
            "focus_page": 7,
            "current_page_main_arc": "Only a partial brief",
            "continuity_resolutions": [],
        }

    monkeypatch.setattr(literature_api, "_call_experience_session_v2_narrative_brief_model", _fake_call)

    with pytest.raises(ValueError, match="narrative brief requires continuity_resolutions"):
        await literature_api._generate_experience_session_v2_narrative_brief(
            reading_dossier=_build_sample_reading_dossier_v2_for_session(),
            focus_page=7,
            reader_profile="curious_generalist",
            user_intent="",
        )


def test_experience_session_v2_bootstrap_should_include_full_dossier_and_v2_cache_namespace():
    dossier = _build_sample_reading_dossier_v2_for_session()
    dossier_sig = literature_api._reading_dossier_v2_signature(dossier)
    cache_key = literature_api._experience_session_v2_cache_key(
        user_id=5,
        paper_id=78,
        focus_page=7,
        selected_kb_id=0,
        dossier_signature=dossier_sig,
        user_intent="build experience",
        reader_profile="curious_generalist",
    )
    session = literature_api._build_experience_session_v2(
        cache_key=cache_key,
        reading_dossier=dossier,
        focus_page=7,
        reader_profile="curious_generalist",
        max_iterations=4,
        max_tool_rounds=6,
        narrative_brief=_build_sample_experience_session_v2_narrative_brief(),
    )

    assert cache_key.startswith(f"{literature_api.EXPERIENCE_SESSION_V2_CACHE_NAMESPACE}:")
    assert session["plan_kind"] == literature_api.EXPERIENCE_SESSION_V2_CACHE_KIND
    assert session["runtime_budget"]["max_iterations"] == 4
    assert session["runtime_budget"]["max_tool_rounds"] == 6
    assert session["resume"]["preferred_strategy"] == "resume"
    assert session["iterations"][0]["phase"] == "bootstrap"
    assert session["iterations"][0]["context_carry"]["mode"] == "full_dossier_bootstrap"
    assert session["iterations"][0]["context_carry"]["full_dossier"]["version"] == "reading_dossier_v2"
    assert session["iterations"][0]["narrative_brief"]["current_page_main_arc"]
    assert session["iterations"][0]["narrative_brief"]["content_strategy"] == "current_page_spine_with_inline_enrichment"
    assert session["meta"]["latest_narrative_brief"]["presentation_strategy"] == "renderer_bound_guided_reading"
    assert session["artifact_promotion"]["promoted_fields"]["narrative_brief"]["focus_page"] == 7


def test_experience_session_v2_bootstrap_should_fail_without_explicit_narrative_brief():
    with pytest.raises(ValueError, match="narrative brief requires current_page_main_arc"):
        literature_api._build_experience_session_v2(
            cache_key="lit:experience_session:v2:test",
            reading_dossier=_build_sample_reading_dossier_v2_for_session(),
            focus_page=7,
            reader_profile="curious_generalist",
            max_iterations=4,
            max_tool_rounds=6,
            narrative_brief={},
        )


def test_experience_session_v2_revise_iteration_should_use_compact_context_carry():
    dossier = _build_sample_reading_dossier_v2_for_session()
    session = literature_api._build_experience_session_v2(
        cache_key="lit:experience_session:v2:test",
        reading_dossier=dossier,
        focus_page=7,
        reader_profile="curious_generalist",
        max_iterations=4,
        max_tool_rounds=6,
        narrative_brief=_build_sample_experience_session_v2_narrative_brief(),
    )
    updated = literature_api._append_experience_session_v2_iteration(
        session,
        phase="revise",
        delta_packet={"updated_sections": ["s1"], "compact_view": "delta-only"},
        state_handle="sess:iter:2",
        tool_trace=[{"round_index": 1, "tool_name": "knowledge_search", "success": True}],
    )

    assert len(updated["iterations"]) == 2
    revise = updated["iterations"][1]
    assert revise["phase"] == "revise"
    assert revise["context_carry"]["mode"] == "delta_state_handle"
    assert revise["context_carry"]["state_handle"] == "sess:iter:2"
    assert revise["context_carry"]["delta_packet"]["compact_view"] == "delta-only"
    assert "adjacent_pages" not in revise["context_carry"]["delta_packet"]
    assert revise["context_carry"]["full_dossier"] is None
    assert revise["narrative_brief"]["current_page_main_arc"]
    assert revise["context_carry"]["delta_packet"]["narrative_brief"]["content_strategy"] == "current_page_spine_with_inline_enrichment"


def test_experience_session_v2_should_fail_loudly_when_narrative_brief_layer_is_missing():
    dossier = _build_sample_reading_dossier_v2_for_session()
    session = literature_api._build_experience_session_v2(
        cache_key="lit:experience_session:v2:test",
        reading_dossier=dossier,
        focus_page=7,
        reader_profile="curious_generalist",
        max_iterations=4,
        max_tool_rounds=6,
        narrative_brief=_build_sample_experience_session_v2_narrative_brief(),
    )
    session["iterations"][0]["narrative_brief"] = None
    session["meta"].pop("latest_narrative_brief", None)

    with pytest.raises(ValueError, match="narrative brief layer missing in session execution"):
        literature_api._append_experience_session_v2_iteration(
            session,
            phase="revise",
            delta_packet={"updated_sections": ["s1"]},
            state_handle="sess:iter:2",
        )


@pytest.mark.asyncio
async def test_experience_session_v2_should_fail_loudly_when_adjacent_continuity_contains_legacy_json_stuffing(monkeypatch):
    dossier = _build_sample_reading_dossier_v2_for_session()
    dossier["adjacent_pages"]["pages"][0]["content_stream"][0]["text"] = (
        '{"page":8,"relation":"next_page","reference_only":true,"summary":"legacy compact summary",'
        '"body_text":"legacy compact body","content_stream":[]}'
    )

    with pytest.raises(ValueError, match="legacy JSON payload stuffing"):
        await literature_api._generate_experience_session_v2_narrative_brief(
            reading_dossier=dossier,
            focus_page=7,
            reader_profile="curious_generalist",
            user_intent="",
        )


def test_experience_session_v2_narrative_brief_should_normalize_string_media_refs():
    brief = literature_api.ExperienceSessionV2NarrativeBrief.model_validate(
        {
            "focus_page": 7,
            "current_page_main_arc": "Use Fig 3 as the main visual anchor for this page.",
            "continuity_resolutions": [
                "Carry forward Page 6's accuracy framing before shifting to DOI."
            ],
            "required_media_refs": [
                "Fig 3 (Concordance and insight density metrics.)",
                {"title": "Figure caption", "description": "Keep caption attached to the figure."},
            ],
            "content_strategy": "figure_first_then_selective_excerpting",
            "presentation_strategy": "keep_figure_and_caption_coupled",
        }
    ).model_dump(mode="json")

    assert brief["required_media_refs"][0]["type"] == "media_ref"
    assert "Fig 3" in brief["required_media_refs"][0]["label"]
    assert brief["required_media_refs"][0]["meta"]["normalized_from"] == "string"
    assert brief["required_media_refs"][1]["label"] == "Figure caption"
    assert brief["required_media_refs"][1]["type"] == "media_ref"


def test_experience_session_v2_narrative_brief_should_preserve_optional_planning_hints():
    brief = literature_api.ExperienceSessionV2NarrativeBrief.model_validate(
        {
            "focus_page": 7,
            "current_page_main_arc": "Use Fig 3 as the main visual anchor for this page.",
            "continuity_resolutions": ["Carry forward Page 6's accuracy framing before shifting to DOI."],
            "required_media_refs": [{"kind": "figure", "page": 7, "layout_id": "layout:7:fig1"}],
            "reader_attention_order": ["Read the Fig 3 comparison first.", "Then use the first results paragraph to interpret DOI."],
            "must_surface_nodes": ["layout:7:fig1", "blk-7-1"],
            "suppressed_threads": ["Do not over-explain adjudication criteria on this page."],
            "content_strategy": "figure_first_then_selective_excerpting",
            "presentation_strategy": "keep_figure_and_caption_coupled",
        }
    ).model_dump(mode="json")

    assert brief["reader_attention_order"][0].startswith("Read the Fig 3")
    assert brief["must_surface_nodes"] == ["layout:7:fig1", "blk-7-1"]
    assert brief["suppressed_threads"] == ["Do not over-explain adjudication criteria on this page."]
    assert brief["opening_key_points"] == []


def test_experience_session_v2_narrative_brief_should_preserve_opening_and_adjacent_bridges():
    brief = literature_api.ExperienceSessionV2NarrativeBrief.model_validate(
        {
            "focus_page": 7,
            "current_page_main_arc": "本页用 Fig 3 把 concordance 和 DOI 放到同一阅读面上。",
            "continuity_resolutions": {
                "from_previous_page": {
                    "page_number": 6,
                    "specific_resolutions": ["上一页先把准确率和判定框架铺好。"],
                    "bridge_to_current_page": "本页顺着上一页的问题，继续问回答是否真的具有教学价值。",
                },
                "to_next_page": {
                    "page_number": 8,
                    "specific_resolutions": ["下一页会把 DOI 带进 discussion。"],
                    "bridge_from_current_page": "本页先把 Fig 3 的结果讲清，下一页再把这些结果转成讨论层解释。",
                },
            },
            "required_media_refs": [{"kind": "figure", "page": 7, "layout_id": "layout:7:fig3"}],
            "opening_points": ["先看 Fig 3 的四个面板。", "再回到正文理解 DOI 为什么重要。"],
            "content_strategy": "figure_first_then_selective_excerpting",
            "presentation_strategy": "keep_figure_and_caption_coupled",
        }
    ).model_dump(mode="json")

    assert brief["opening_key_points"][0].startswith("先看 Fig 3")
    assert brief["previous_page_bridge"]["page_number"] == 6
    assert brief["next_page_bridge"]["page_number"] == 8


def test_experience_session_v2_narrative_brief_should_normalize_nested_strategy_aliases():
    brief = literature_api.ExperienceSessionV2NarrativeBrief.model_validate(
        {
            "focus_page": 7,
            "reading_strategy": {
                "main_arc": "先用 Figure 3 建立当前页主问题，再把 DOI 作为第二主线。",
                "continuity_notes": ["只保留来自上一页的方法学过渡，不要让它压住本页主发现。"],
                "required_media": {"kind": "figure", "page": 7, "layout_id": "layout:7:fig3"},
                "opening_takeaways": ["先抓 Fig 3。", "再回正文读 DOI。"],
                "from_previous_page": {
                    "page": 6,
                    "key_points": ["上一页先铺了准确率结果。"],
                    "bridge_text": "本页接着问这些正确答案是否有教学价值。",
                },
                "to_next_page": {
                    "page": 8,
                    "key_points": ["下一页会把 DOI 带入 discussion。"],
                    "bridge_text": "本页先把结果看清，下一页再解释它意味着什么。",
                },
                "content_plan": {"sequence": ["figure_first", "short_excerpt_then_explain"]},
                "presentation_plan": {"layout": "mixed_layout", "density": "moderate"},
                "reader_steps": ["先看 Fig 3 总体比较。", "再看 DOI 结果。"],
            },
        }
    ).model_dump(mode="json")

    assert brief["current_page_main_arc"].startswith("先用 Figure 3")
    assert brief["continuity_resolutions"][0].startswith("只保留来自上一页")
    assert brief["required_media_refs"][0]["label"]
    assert brief["content_strategy"]["sequence"][0] == "figure_first"
    assert brief["presentation_strategy"]["layout"] == "mixed_layout"
    assert brief["reader_attention_order"][0].startswith("先看 Fig 3")
    assert brief["opening_key_points"][0] == "先抓 Fig 3。"
    assert brief["previous_page_bridge"]["page"] == 6
    assert brief["next_page_bridge"]["page"] == 8


def test_experience_session_v2_artifact_draft_should_require_original_excerpt_display_text_and_source_ids():
    with pytest.raises(ValueError, match="original_excerpt nodes require display_text"):
        literature_api.ExperienceSessionV2ArtifactDraft.model_validate(
            {
                "focus_page": 7,
                "template_hint": "guided_mixed_media_v1",
                "layout_recipe": "current_page_spine_interleave_v1",
                "presentation_mode": "mixed_layout",
                "nodes": [
                    {
                        "node_kind": "original_excerpt",
                        "source_layout_ids": ["layout:7:1"],
                    }
                ],
                "resource_requests": [],
            }
        )

    with pytest.raises(ValueError, match="original_excerpt nodes require source ids"):
        literature_api.ExperienceSessionV2ArtifactDraft.model_validate(
            {
                "focus_page": 7,
                "template_hint": "guided_mixed_media_v1",
                "layout_recipe": "current_page_spine_interleave_v1",
                "presentation_mode": "mixed_layout",
                "nodes": [
                    {
                        "node_kind": "original_excerpt",
                        "display_text": "We first examined the frequency of insight.",
                    }
                ],
                "resource_requests": [],
            }
        )


def test_experience_session_v2_artifact_draft_should_normalize_common_model_alias_fields():
    draft = literature_api.ExperienceSessionV2ArtifactDraft.model_validate(
        {
            "focus_page": 7,
            "template_id": "guided_mixed_media_v1",
            "layout": "current_page_spine_interleave_v1",
            "presentation": "mixed_layout",
            "nodes": [
                {
                    "node_id": "n1",
                    "type": "heading",
                    "title": "先看图中的核心比较。",
                },
                {
                    "node_id": "n2",
                    "node_kind": "original_excerpt",
                    "excerpt": "We first examined the frequency of insight.",
                    "translation": "我们首先考察了洞见出现的频率。",
                    "source_layout_id": "layout:7:1",
                    "source_block_id": "blk-7-1",
                },
                {
                    "node_id": "n3",
                    "node_kind": "paragraph",
                    "content": "这一段把图里的比较关系和正文里的 DOI 叙述接起来。",
                },
                {
                    "node_id": "n4",
                    "node_kind": "figure_slot",
                    "label": "Figure 3",
                    "source_layout_id": "layout:7:fig1",
                    "description": "把 concordance 和 DOI 图一起浮出来。",
                },
                {
                    "node_id": "n5",
                    "node_kind": "aside",
                    "body": "这里提醒读者 DOI 是这页真正的新变量。",
                },
                {
                    "node_id": "n6",
                    "node_kind": "external_resource",
                    "label": "Paper URL",
                    "resources": [
                        {
                            "resource_id": "seed:1",
                            "url": "https://example.com/paper",
                            "renderable": True,
                        }
                    ],
                },
            ],
            "retrieval_requests": [],
        }
    ).model_dump(mode="json")

    assert draft["template_hint"] == "guided_mixed_media_v1"
    assert draft["layout_recipe"] == "current_page_spine_interleave_v1"
    assert draft["presentation_mode"] == "mixed_layout"
    assert draft["nodes"][0]["text"] == "先看图中的核心比较。"
    assert draft["nodes"][1]["display_text"] == "We first examined the frequency of insight."
    assert draft["nodes"][1]["translation_zh"] == "我们首先考察了洞见出现的频率。"
    assert draft["nodes"][1]["source_layout_ids"] == ["layout:7:1"]
    assert draft["nodes"][1]["source_block_ids"] == ["blk-7-1"]
    assert draft["nodes"][2]["text"].startswith("这一段把图里的比较关系")
    assert draft["nodes"][3]["source_layout_ids"] == ["layout:7:fig1"]
    assert draft["nodes"][4]["text"].startswith("这里提醒读者 DOI")
    assert draft["nodes"][5]["resource_ref_ids"] == ["seed:1"]


def test_experience_session_v2_artifact_draft_should_preserve_grouping_meta_alias_fields():
    draft = literature_api.ExperienceSessionV2ArtifactDraft.model_validate(
        {
            "focus_page": 7,
            "template_hint": "guided_mixed_media_v1",
            "layout_recipe": "current_page_spine_interleave_v1",
            "presentation_mode": "mixed_layout",
            "nodes": [
                {
                    "node_kind": "paragraph",
                    "text": "先确定图里的主要比较。",
                    "group_id": "g-intro",
                    "group_label": "Figure 3 core read",
                    "lane": "main",
                    "placement": "block",
                    "prominence": "hero",
                }
            ],
            "resource_requests": [],
        }
    ).model_dump(mode="json")

    assert draft["nodes"][0]["meta"]["group_id"] == "g-intro"
    assert draft["nodes"][0]["meta"]["group_label"] == "Figure 3 core read"
    assert draft["nodes"][0]["meta"]["lane"] == "main"
    assert draft["nodes"][0]["meta"]["placement"] == "block"
    assert draft["nodes"][0]["meta"]["prominence"] == "hero"


def test_experience_session_v2_artifact_draft_should_normalize_structured_top_level_planning_fields():
    draft = literature_api.ExperienceSessionV2ArtifactDraft.model_validate(
        {
            "focus_page": 7,
            "template_hint": {
                "id": "guided_mixed_media_v1",
                "reason": "figure_first_reader_page",
            },
            "layout_recipe": {
                "main_lane_width": "wide",
                "support_lane": "compact",
                "excerpt_density": "moderate",
            },
            "presentation_mode": {
                "mode": "mixed_layout",
                "emphasis": "figure_first",
            },
            "nodes": [
                {
                    "node_kind": "paragraph",
                    "text": "先看 Figure 3，再用中文解释各个指标的关系。",
                }
            ],
            "resource_requests": [],
        }
    ).model_dump(mode="json")

    assert draft["template_hint"] == "guided_mixed_media_v1"
    assert draft["layout_recipe"] == "main_lane_width:wide|support_lane:compact|excerpt_density:moderate"
    assert draft["presentation_mode"] == "mixed_layout"
    assert draft["meta"]["template_hint_config"]["reason"] == "figure_first_reader_page"
    assert draft["meta"]["layout_recipe_config"]["support_lane"] == "compact"
    assert draft["meta"]["presentation_mode_config"]["emphasis"] == "figure_first"


def test_experience_session_v2_artifact_draft_prompt_should_include_dossier_brief_and_resource_bundle():
    dossier = _build_sample_reading_dossier_v2_for_session()
    session = literature_api._build_experience_session_v2(
        cache_key="lit:experience_session:v2:test",
        reading_dossier=dossier,
        focus_page=7,
        reader_profile="curious_generalist",
        max_iterations=4,
        max_tool_rounds=6,
        narrative_brief=_build_sample_experience_session_v2_narrative_brief(),
    )
    resource_bundle = literature_api._build_reader_v2_seed_resource_bundle(
        paper=SimpleNamespace(
            url="https://example.com/paper",
            doi="10.1000/demo",
            arxiv_url="https://arxiv.org/abs/1234.5678",
            pdf_url="https://example.com/paper.pdf",
        ),
        compose_payload=_build_sample_compose_payload_for_dossier_v2(),
        narrative_brief=session["meta"]["latest_narrative_brief"],
    )

    payload = literature_api._build_experience_session_v2_artifact_draft_prompt_payload(
        reading_dossier=dossier,
        session_payload=session,
        resource_bundle=resource_bundle,
        include_full_dossier=True,
    )

    assert payload["reading_dossier_v2"]["current_page"]["owner"] == "compose/page_grounding_v1"
    assert len(payload["reading_dossier_v2"]["adjacent_pages"]["pages"][0]["content_stream"]) > 2
    assert payload["narrative_brief"]["current_page_main_arc"]
    assert "reader_attention_order" in payload["narrative_brief"]
    assert payload["resource_bundle"]["bundle_entries"]
    assert payload["rules"]["reader_facing_language"] == "zh-CN"
    assert payload["teaching_sequence_preferences"]["target_shape"] == "ordered_teaching_node_sequence"
    assert payload["teaching_sequence_preferences"]["authored_language"] == "zh-CN"
    assert payload["anchor_excerpt_candidates"]


def test_experience_session_v2_prompts_should_require_chinese_guided_copy():
    brief_prompt = literature_api._experience_session_v2_narrative_brief_system_prompt()
    draft_prompt = literature_api._experience_session_v2_artifact_draft_system_prompt()

    assert "Simplified Chinese" in brief_prompt
    assert "Simplified Chinese" in draft_prompt
    assert "opening_key_points" in brief_prompt
    assert "previous_page_bridge" in brief_prompt
    assert "next_page_bridge" in brief_prompt
    assert '"translation_zh":"..."' in draft_prompt
    assert "do not translate excerpts into Chinese" not in draft_prompt
    assert "provide translation_zh as a faithful Simplified Chinese translation" in draft_prompt


def test_reader_experience_block_explain_request_should_require_local_context():
    with pytest.raises(ValueError):
        literature_api.ReaderExperienceBlockExplainRequest(
            page=7,
            block_id="seg-1",
            explain_kind="simplify",
            question="请讲得更通俗一点",
        )

    with pytest.raises(ValueError):
        literature_api.ReaderExperienceBlockExplainRequest(
            page=7,
            block_id="seg-fig-1",
            explain_kind="figure",
            question="请解释这张图",
        )


def test_reader_experience_block_explain_prompt_should_use_local_block_materials():
    payload = literature_api.ReaderExperienceBlockExplainRequest(
        page=7,
        block_id="seg-7-para",
        explain_kind="simplify",
        question="请把这一段讲得更通俗一点",
        source_excerpt="We first examined the frequency of insight.",
        source_translation_zh="我们首先考察了洞察出现的频率。",
        explanation_text="这里在把 insight prevalence 作为质量评估的入口。",
        history=[
            literature_api.ReaderExperienceBlockExplainTurn(
                role="assistant",
                content="这段主要是在解释作者如何开始衡量洞察出现频率。",
            )
        ],
    )

    system_prompt = literature_api._reader_experience_block_explain_system_prompt(payload.explain_kind)
    messages = literature_api._build_reader_experience_block_explain_messages(payload)

    assert "不要提知识库" in system_prompt
    assert "原文摘录：We first examined the frequency of insight." in messages[0]["content"]
    assert "原文中文译文：我们首先考察了洞察出现的频率。" in messages[0]["content"]
    assert "当前讲读：这里在把 insight prevalence 作为质量评估的入口。" in messages[0]["content"]
    assert messages[1]["role"] == "assistant"
    assert messages[-1]["content"] == "请把这一段讲得更通俗一点"


def test_reader_experience_block_explain_messages_should_include_figure_image_payload():
    payload = literature_api.ReaderExperienceBlockExplainRequest(
        page=7,
        block_id="seg-fig-3",
        explain_kind="figure",
        question="请只解释这张图",
        figure_label="Fig 3",
        figure_caption="Concordance and insight of ChatGPT on USMLE.",
        figure_text="Fig 3",
        figure_image_url="data:image/png;base64,ZmFrZQ==",
    )

    messages = literature_api._build_reader_experience_block_explain_messages(payload)

    assert isinstance(messages[0]["content"], list)
    assert messages[0]["content"][0]["type"] == "image_url"
    assert messages[0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert messages[0]["content"][1]["type"] == "text"
    assert "图块标签：Fig 3" in messages[0]["content"][1]["text"]
    assert messages[-1]["content"] == "请只解释这张图"


@pytest.mark.asyncio
async def test_normalize_reader_experience_block_explain_image_url_should_inline_local_figure_asset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    upload_dir = tmp_path / "uploads"
    asset_dir = upload_dir / "reader_figure_assets" / "78" / "p7"
    asset_dir.mkdir(parents=True, exist_ok=True)
    asset_path = asset_dir / "asset-123.jpg"
    asset_bytes = b"fake-jpeg-bytes"
    asset_path.write_bytes(asset_bytes)
    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))

    paper = SimpleNamespace(id=78, user_id=1)
    normalized = await literature_api._normalize_reader_experience_block_explain_image_url(
        db=_FakeDB([]),
        paper=paper,
        raw_url="http://localhost:8888/api/v1/literature/reader/figure-assets/78/7/asset-123",
    )

    assert normalized.startswith("data:image/jpeg;base64,")
    assert normalized.endswith("ZmFrZS1qcGVnLWJ5dGVz")


@pytest.mark.asyncio
async def test_normalize_reader_experience_block_explain_image_url_should_reject_cross_paper_asset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))

    paper = SimpleNamespace(id=78, user_id=1)
    with pytest.raises(ValueError, match="与当前论文不匹配"):
        await literature_api._normalize_reader_experience_block_explain_image_url(
            db=_FakeDB([]),
            paper=paper,
            raw_url="http://localhost:8888/api/v1/literature/reader/figure-assets/99/7/asset-123",
        )


def test_friendly_reader_experience_block_explain_error_message_should_clarify_invalid_image_url():
    error = Exception(
        "Error code: 400 - {'error': {'message': '<400> InternalError.Algo.InvalidParameter: "
        "The provided URL does not appear to be valid. Ensure it is correctly formatted.'}}"
    )

    message = literature_api._friendly_reader_experience_block_explain_error_message(error)

    assert message == "当前图块图片地址无效，模型无法读取。请刷新当前页后重试。"


@pytest.mark.asyncio
async def test_create_reader_experience_block_explain_stream_should_disable_thinking_when_supported():
    calls: list[dict[str, object]] = []
    sentinel = object()

    class _FakeCompletions:
        async def create(self, **kwargs):
            calls.append(dict(kwargs))
            return sentinel

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()))

    stream = await literature_api._create_reader_experience_block_explain_stream(
        client=fake_client,
        request_kwargs={"model": "qwen3.5-plus", "messages": [], "stream": True},
    )

    assert stream is sentinel
    assert len(calls) == 1
    assert calls[0]["extra_body"] == {"enable_thinking": False}


@pytest.mark.asyncio
async def test_create_reader_experience_block_explain_stream_should_fallback_when_disable_thinking_unsupported():
    calls: list[dict[str, object]] = []
    sentinel = object()

    class _FakeCompletions:
        async def create(self, **kwargs):
            calls.append(dict(kwargs))
            if len(calls) == 1:
                raise Exception("invalid_request_error: enable_thinking is not supported")
            return sentinel

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()))

    stream = await literature_api._create_reader_experience_block_explain_stream(
        client=fake_client,
        request_kwargs={"model": "qwen3.5-plus", "messages": [], "stream": True},
    )

    assert stream is sentinel
    assert len(calls) == 2
    assert calls[0]["extra_body"] == {"enable_thinking": False}
    assert "extra_body" not in calls[1]


def test_page_artifact_v2_compact_source_context_should_split_long_current_page_excerpt_candidates():
    dossier = _build_sample_reading_dossier_v2_for_session()
    long_text = (
        "We first examined the frequency (prevalence) of insight. Overall, ChatGPT produced at least one significant "
        "insight in 88.9% of all responses. Insight frequency was generally consistent between exam type and question "
        "input format (Fig 3C). In Step 2CK however, insight decreased by 10.3% (n = 11 items) between MC-NJ and MC-J "
        "formulations, paralleling the decrement in accuracy (Fig 1B). Review of this subset of questions did not "
        "reveal a discernible pattern. Next, we quantified the density of insight (DOI) contained within AI-generated "
        "explanations. A density index was defined by normalizing the number of unique insights against the number of "
        "possible answer choices. This analysis was performed on MC-J entries only. High quality outputs were generally "
        "characterized by DOI > 0.6."
    )
    dossier["current_page"]["rich_grounding"]["reading_nodes"] = [
        {
            "node_id": "layout:7:long",
            "node_kind": "paragraph",
            "clean_text": long_text,
            "normalized_text": long_text,
            "raw_text": long_text,
            "source_layout_ids": ["layout:7:long"],
            "source_block_ids": [f"blk-{idx}" for idx in range(1, 11)],
            "include_in_main_flow": True,
            "meta": {"layout_type": "text"},
        }
    ]
    dossier["current_page"]["rich_grounding"]["layout_atoms"] = []

    compact = literature_api._build_page_artifact_v2_compact_source_context(
        reading_dossier=dossier,
        focus_page=7,
    )

    candidates = compact["excerpt_candidates"]
    assert len(candidates) == 2
    assert candidates[0]["source_block_ids"] == [f"blk-{idx}" for idx in range(1, 7)]
    assert candidates[1]["source_block_ids"] == [f"blk-{idx}" for idx in range(7, 11)]
    assert candidates[1]["display_text"].startswith("Next, we quantified the density of insight")


def test_experience_session_v2_artifact_draft_node_should_normalize_reader_role_aliases():
    draft = literature_api.ExperienceSessionV2ArtifactDraft.model_validate(
        {
            "focus_page": 7,
            "template_hint": "guided_mixed_media_v1",
            "layout_recipe": "current_page_spine_interleave_v1",
            "presentation_mode": "mixed_layout",
            "nodes": [
                {
                    "node_kind": "original_excerpt",
                    "display_text": "We first examined the frequency of insight.",
                    "source_layout_id": "layout:7:1",
                    "source_block_id": "blk-7-1",
                    "role": "excerpt",
                },
                {
                    "node_kind": "paragraph",
                    "text": "先解释这句原文为什么重要。",
                    "reader_role": "explanation",
                },
            ],
            "resource_requests": [],
        }
    ).model_dump(mode="json")

    assert draft["nodes"][0]["meta"]["reader_role"] == "anchor_excerpt"
    assert draft["nodes"][1]["meta"]["reader_role"] == "teaching_explanation"


def test_experience_session_v2_artifact_draft_should_require_some_teaching_paragraph_when_excerpt_exists():
    with pytest.raises(ValueError, match="requires teaching paragraphs alongside selected excerpts"):
        literature_api.ExperienceSessionV2ArtifactDraft.model_validate(
            {
                "focus_page": 7,
                "template_hint": "guided_mixed_media_v1",
                "layout_recipe": "current_page_spine_interleave_v1",
                "presentation_mode": "mixed_layout",
                "nodes": [
                    {
                        "node_kind": "original_excerpt",
                        "display_text": "We first examined the frequency of insight.",
                        "source_layout_ids": ["layout:7:1"],
                        "source_block_ids": ["blk-7-1"],
                    },
                    {
                        "node_kind": "figure_slot",
                        "label": "Figure 3",
                        "source_layout_ids": ["layout:7:fig1"],
                    },
                ],
                "resource_requests": [],
            }
        )


def test_experience_session_v2_artifact_draft_should_allow_excerpt_when_teaching_paragraph_exists_elsewhere_in_sequence():
    draft = literature_api.ExperienceSessionV2ArtifactDraft.model_validate(
        {
            "focus_page": 7,
            "template_hint": "guided_mixed_media_v1",
            "layout_recipe": "current_page_spine_interleave_v1",
            "presentation_mode": "mixed_layout",
            "nodes": [
                {
                    "node_kind": "heading",
                    "text": "Fig 3",
                },
                {
                    "node_kind": "paragraph",
                    "text": "先给读者一个总导读，再进入原文证据。",
                },
                {
                    "node_kind": "original_excerpt",
                    "display_text": "We first examined the frequency of insight.",
                    "source_layout_ids": ["layout:7:1"],
                    "source_block_ids": ["blk-7-1"],
                },
                {
                    "node_kind": "figure_slot",
                    "label": "Figure 3",
                    "source_layout_ids": ["layout:7:fig1"],
                },
            ],
            "resource_requests": [],
        }
    ).model_dump(mode="json")

    assert [node["node_kind"] for node in draft["nodes"][:3]] == ["heading", "paragraph", "original_excerpt"]


def test_experience_session_v2_artifact_draft_should_allow_excerpt_cluster_followed_by_paragraph():
    draft = literature_api.ExperienceSessionV2ArtifactDraft.model_validate(
        {
            "focus_page": 7,
            "template_hint": "guided_mixed_media_v1",
            "layout_recipe": "current_page_spine_interleave_v1",
            "presentation_mode": "mixed_layout",
            "nodes": [
                {
                    "node_kind": "heading",
                    "text": "Insight prevalence",
                    "meta": {"section_id": "sec-1"},
                },
                {
                    "node_kind": "original_excerpt",
                    "display_text": "We first examined the frequency of insight.",
                    "source_layout_ids": ["layout:7:1"],
                    "source_block_ids": ["blk-7-1"],
                    "meta": {"group_id": "grp-1"},
                },
                {
                    "node_kind": "original_excerpt",
                    "display_text": "Overall, ChatGPT produced at least one significant insight in 88.9% of all responses.",
                    "source_layout_ids": ["layout:7:1"],
                    "source_block_ids": ["blk-7-2"],
                    "meta": {"group_id": "grp-1"},
                },
                {
                    "node_kind": "figure_slot",
                    "label": "Figure 3",
                    "source_layout_ids": ["layout:7:fig1"],
                    "meta": {"group_id": "grp-1"},
                },
                {
                    "node_kind": "paragraph",
                    "text": "这两句原文一起建立了本页最重要的 insight prevalence 结论。",
                    "meta": {"group_id": "grp-1"},
                },
            ],
            "resource_requests": [],
        }
    ).model_dump(mode="json")

    assert [node["node_kind"] for node in draft["nodes"][:3]] == ["heading", "original_excerpt", "original_excerpt"]


def test_experience_session_v2_artifact_draft_should_normalize_resource_request_alias_fields():
    draft = literature_api.ExperienceSessionV2ArtifactDraft.model_validate(
        {
            "focus_page": 7,
            "template_hint": "guided_mixed_media_v1",
            "layout_recipe": "current_page_spine_interleave_v1",
            "presentation_mode": "mixed_layout",
            "nodes": [{"node_kind": "paragraph", "text": "先确定图里的主要比较。"}],
            "resource_requests": [
                {
                    "tool": "web_search",
                    "q": "USMLE official overview",
                    "reason": "Need official context",
                }
            ],
        }
    ).model_dump(mode="json")

    assert draft["resource_requests"][0]["tool_name"] == "web_search"
    assert draft["resource_requests"][0]["query"] == "USMLE official overview"
    assert draft["resource_requests"][0]["request_id"].startswith("req-")


@pytest.mark.asyncio
async def test_experience_session_v2_artifact_draft_generation_should_fail_loudly_on_invalid_output(monkeypatch):
    dossier = _build_sample_reading_dossier_v2_for_session()
    session = literature_api._build_experience_session_v2(
        cache_key="lit:experience_session:v2:test",
        reading_dossier=dossier,
        focus_page=7,
        reader_profile="curious_generalist",
        max_iterations=4,
        max_tool_rounds=6,
        narrative_brief=_build_sample_experience_session_v2_narrative_brief(),
    )
    resource_bundle = literature_api._build_reader_v2_seed_resource_bundle(
        paper=SimpleNamespace(url="https://example.com/paper", doi="10.1000/demo", arxiv_url="", pdf_url=""),
        compose_payload=_build_sample_compose_payload_for_dossier_v2(),
        narrative_brief=session["meta"]["latest_narrative_brief"],
    )

    monkeypatch.setattr(
        literature_api,
        "_call_experience_session_v2_artifact_draft_model",
        lambda **_kwargs: asyncio.sleep(0, result={"focus_page": 7}),
    )

    with pytest.raises(ValueError, match="missing required draft fields"):
        await literature_api._generate_experience_session_v2_artifact_draft(
            reading_dossier=dossier,
            session_payload=session,
            resource_bundle=resource_bundle,
            include_full_dossier=True,
        )


@pytest.mark.asyncio
async def test_run_reader_experience_v2_artifact_drafting_loop_should_support_multiple_retrieval_rounds(monkeypatch):
    dossier = _build_sample_reading_dossier_v2_for_session()
    paper = SimpleNamespace(
        id=78,
        title="Demo Paper",
        url="https://example.com/paper",
        doi="10.1000/demo",
        arxiv_url="https://arxiv.org/abs/1234.5678",
        pdf_url="https://example.com/paper.pdf",
        pdf_path="demo.pdf",
    )
    session = literature_api._build_experience_session_v2(
        cache_key="lit:experience_session:v2:test",
        reading_dossier=dossier,
        focus_page=7,
        reader_profile="curious_generalist",
        max_iterations=4,
        max_tool_rounds=4,
        narrative_brief=_build_sample_experience_session_v2_narrative_brief(),
    )
    draft_queue = [
        _build_sample_experience_session_v2_artifact_draft(
            resource_requests=[
                {
                    "request_id": "req-1",
                    "tool_name": "web_search",
                    "query": "USMLE official overview",
                    "reason": "Need official context",
                    "max_results": 2,
                }
            ],
            nodes=[
                {
                    "node_kind": "original_excerpt",
                    "display_text": "We first examined the frequency of insight.",
                    "source_layout_ids": ["layout:7:1"],
                    "source_block_ids": ["blk-7-1"],
                },
                {"node_kind": "paragraph", "text": "先确定图里的主要比较。"},
                {"node_kind": "figure_slot", "label": "Figure 3", "source_layout_ids": ["layout:7:fig1"]},
                {"node_kind": "term_note", "term": "Insight", "definition": "Brief definition."},
            ],
        ),
        _build_sample_experience_session_v2_artifact_draft(
            resource_requests=[
                {
                    "request_id": "req-2",
                    "tool_name": "web_scrape",
                    "url": "https://example.com/usmle",
                    "reason": "Need concise official summary",
                    "max_results": 1,
                }
            ],
            nodes=[
                {
                    "node_kind": "original_excerpt",
                    "display_text": "We first examined the frequency of insight.",
                    "source_layout_ids": ["layout:7:1"],
                    "source_block_ids": ["blk-7-1"],
                },
                {"node_kind": "paragraph", "text": "先看当前页图，再补官方概览。"},
                {"node_kind": "figure_slot", "label": "Figure 3", "source_layout_ids": ["layout:7:fig1"]},
                {"node_kind": "term_note", "term": "Insight", "definition": "Brief definition."},
            ],
        ),
            _build_sample_experience_session_v2_artifact_draft(
                nodes=[
                    {"node_kind": "heading", "text": "Figure 3 先行"},
                    {
                        "node_kind": "original_excerpt",
                        "display_text": "We first examined the frequency of insight.",
                        "source_layout_ids": ["layout:7:1"],
                        "source_block_ids": ["blk-7-1"],
                    },
                    {"node_kind": "paragraph", "text": "先用图把 concordance 对齐，再回到正文解释 insight。"},
                    {
                        "node_kind": "figure_slot",
                        "label": "Figure 3",
                    "caption": "Use the figure as the main visual anchor.",
                    "source_layout_ids": ["layout:7:fig1"],
                },
                {"node_kind": "term_note", "term": "Insight", "definition": "Brief definition."},
                {
                    "node_kind": "external_resource",
                    "label": "USMLE overview",
                    "resource_ref_ids": [
                        literature_api._build_reader_v2_resource_entry_id(
                            "web_search",
                            "req-1",
                            1,
                            "https://example.com/usmle",
                            "USMLE overview",
                        )
                    ],
                },
            ],
            resource_requests=[],
        ),
    ]

    async def _fake_generate(**_kwargs):
        return draft_queue.pop(0)

    class _FakeRegistry:
        async def execute(self, tool_name: str, **kwargs):
            if tool_name == "web_search":
                return literature_api.ToolResult(
                    success=True,
                    output="search ok",
                    data={
                        "results": [
                            {
                                "title": "USMLE overview",
                                "url": "https://example.com/usmle",
                                "snippet": "Official overview for test prep",
                            }
                        ]
                    },
                )
            return literature_api.ToolResult(
                success=True,
                output="scrape ok",
                data={"url": "https://example.com/usmle", "summary": "Official overview for test prep"},
            )

    async def _fake_build_registry(**_kwargs):
        return _FakeRegistry(), {"web_search", "web_scrape", "paper_read", "knowledge_search"}

    monkeypatch.setattr(literature_api, "_generate_experience_session_v2_artifact_draft", _fake_generate)
    monkeypatch.setattr(literature_api, "_build_generative_reader_agent_tool_registry_for_paper", _fake_build_registry)

    updated_session, resource_bundle, artifact_draft, authored_plan = await literature_api._run_reader_experience_v2_artifact_drafting_loop(
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
        paper=paper,
        selected_kb_id=0,
        compose_payload=_build_sample_compose_payload_for_dossier_v2(),
        reading_dossier=dossier,
        session_payload=session,
    )

    assert updated_session["meta"]["latest_artifact_draft"]["meta"]["generator_mode"] == "model_generated_artifact_draft"
    assert updated_session["meta"]["latest_resource_bundle"]["meta"]["retrieval_rounds"] == 2
    assert len(updated_session["iterations"]) == 3
    assert resource_bundle["external_resources"]
    assert artifact_draft["nodes"][-1]["node_kind"] == "external_resource"
    assert authored_plan["external_resources"]


@pytest.mark.asyncio
async def test_execute_experience_v2_artifact_resource_requests_should_treat_empty_kb_result_as_nonfatal(monkeypatch):
    paper = SimpleNamespace(id=78, title="Demo Paper", pdf_path="demo.pdf")
    seed_bundle = {
        "bundle_entries": [],
        "external_resources": [],
        "meta": {"retrieval_rounds": 0},
    }

    class _FakeRegistry:
        async def execute(self, tool_name: str, **_kwargs):
            assert tool_name == "knowledge_search"
            return literature_api.ToolResult(
                success=False,
                output="在当前论文范围内未检索到可用片段。",
                data={"results": [], "total": 0},
                error="no_results",
            )

    async def _fake_build_registry(**_kwargs):
        return _FakeRegistry(), {"knowledge_search", "paper_read", "web_search", "web_scrape"}

    monkeypatch.setattr(literature_api, "_build_generative_reader_agent_tool_registry_for_paper", _fake_build_registry)

    merged_bundle, tool_trace = await literature_api._execute_experience_v2_artifact_resource_requests(
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
        paper=paper,
        selected_kb_id=84,
        requests=[
            {
                "request_id": "req-kb-1",
                "tool_name": "knowledge_search",
                "query": "洞察密度 DOI",
                "reason": "Need kb context",
                "max_results": 3,
            }
        ],
        resource_bundle=seed_bundle,
    )

    assert merged_bundle["meta"]["retrieval_rounds"] == 1
    assert merged_bundle["bundle_entries"] == []
    assert merged_bundle["external_resources"] == []
    feedback = merged_bundle["meta"]["nonfatal_request_feedback"]
    assert feedback[0]["tool_name"] == "knowledge_search"
    assert feedback[0]["status"] == "no_results"
    assert tool_trace[0]["tool_name"] == "knowledge_search"
    assert tool_trace[0]["meta"]["nonfatal_empty_result"] is True


def test_page_artifact_v2_authored_plan_should_keep_neighboring_continuity_latent():
    dossier = _build_sample_reading_dossier_v2_for_session()
    cache_key = literature_api._experience_session_v2_cache_key(
        user_id=5,
        paper_id=78,
        focus_page=7,
        selected_kb_id=0,
        dossier_signature=literature_api._reading_dossier_v2_signature(dossier),
        user_intent="build experience",
        reader_profile="curious_generalist",
    )
    session = literature_api._build_experience_session_v2(
        cache_key=cache_key,
        reading_dossier=dossier,
        focus_page=7,
        reader_profile="curious_generalist",
        max_iterations=4,
        max_tool_rounds=6,
        narrative_brief=_build_sample_experience_session_v2_narrative_brief(),
    )
    session["meta"]["latest_narrative_brief"]["continuity_resolutions"] = [
        "previous_page:6: prior context",
        "next_page:8: following context",
    ]
    session["meta"]["latest_artifact_draft"] = _build_sample_experience_session_v2_artifact_draft(
        nodes=[
            {
                "node_kind": "original_excerpt",
                "display_text": "We first examined the frequency of insight.",
                "source_layout_ids": ["layout:7:1"],
                "source_block_ids": ["blk-7-1"],
            },
            {
                "node_kind": "paragraph",
                "text": "先把这一页的比较主线拉直，再把邻页上下文吸收到当前页解释里。",
            },
            {
                "node_kind": "figure_slot",
                "label": "Figure 3",
                "caption": "把图里的比较放回当前页主线。",
                "source_layout_ids": ["layout:7:fig1"],
            },
            {
                "node_kind": "term_note",
                "term": "Insight",
                "definition": "当前页只保留和比较关系最相关的解释。",
            },
            {
                "node_kind": "external_resource",
                "label": "Paper URL",
                "resource_ref_ids": ["seed:1"],
            },
        ],
    )
    session["meta"]["latest_resource_bundle"] = literature_api._build_reader_v2_seed_resource_bundle(
        paper=SimpleNamespace(
            url="https://example.com/paper",
            doi="10.1000/demo",
            arxiv_url="https://arxiv.org/abs/1234.5678",
            pdf_url="https://example.com/paper.pdf",
        ),
        compose_payload=_build_sample_compose_payload_for_dossier_v2(),
        narrative_brief=session["meta"]["latest_narrative_brief"],
    )
    paper = SimpleNamespace(
        url="https://example.com/paper",
        doi="10.1000/demo",
        arxiv_url="https://arxiv.org/abs/1234.5678",
        pdf_url="https://example.com/paper.pdf",
    )

    resource_bundle, authored_plan = literature_api._build_page_artifact_v2_authored_plan_from_session(
        paper=paper,
        compose_payload=_build_sample_compose_payload_for_dossier_v2(),
        reading_dossier=dossier,
        session_payload=session,
    )

    authored_text = " ".join(list(authored_plan.get("authored_explanations") or []))
    assert "previous_page:" not in authored_text
    assert "next_page:" not in authored_text
    assert "吸收到当前页解释里" in authored_text
    assert authored_plan["excerpt_overrides"][0]["display_text"] == "We first examined the frequency of insight."
    assert authored_plan["requested_node_kinds"] == [
        "external_resource",
        "figure_slot",
        "original_excerpt",
        "paragraph",
        "term_annotation",
    ]
    assert resource_bundle["continuity_resolutions"]


def test_page_artifact_v2_authored_plan_should_extract_structured_main_arc_from_rich_brief():
    dossier = _build_sample_reading_dossier_v2_for_session()
    cache_key = literature_api._experience_session_v2_cache_key(
        user_id=5,
        paper_id=78,
        focus_page=8,
        selected_kb_id=0,
        dossier_signature=literature_api._reading_dossier_v2_signature(dossier),
        user_intent="build experience",
        reader_profile="curious_generalist",
    )
    session = literature_api._build_experience_session_v2(
        cache_key=cache_key,
        reading_dossier=dossier,
        focus_page=8,
        reader_profile="curious_generalist",
        max_iterations=4,
        max_tool_rounds=6,
        narrative_brief=_build_sample_rich_experience_session_v2_narrative_brief(),
    )
    session["meta"]["latest_artifact_draft"] = _build_sample_experience_session_v2_artifact_draft(
        nodes=[
            {
                "node_kind": "original_excerpt",
                "display_text": "We first examined the frequency of insight.",
                "source_layout_ids": ["layout:7:1"],
                "source_block_ids": ["blk-7-1"],
            },
            {
                "node_kind": "paragraph",
                "text": "先把 discussion 的起点和上一页的 Fig 3D 连起来。",
            },
            {
                "node_kind": "figure_slot",
                "label": "Figure 3",
                "caption": "继续使用上一页已经建立的 Fig 3 语境。",
                "source_layout_ids": ["layout:7:fig1"],
            },
            {
                "node_kind": "term_note",
                "term": "DOI",
                "definition": "指非显而易见的洞见频率。",
            },
            {
                "node_kind": "external_resource",
                "label": "USMLE overview",
                "resource_ref_ids": ["web:1"],
            },
        ],
    )
    session["meta"]["latest_resource_bundle"] = {
        "bundle_entries": [
            {
                "resource_id": "web:1",
                "label": "USMLE overview",
                "url": "https://example.com/usmle",
                "resource_type": "web_search",
                "summary": "Official-style overview",
                "source_tool": "web_search",
                "renderable": True,
            }
        ],
        "external_resources": [
            {
                "resource_id": "web:1",
                "label": "USMLE overview",
                "url": "https://example.com/usmle",
                "resource_type": "web_search",
            }
        ],
        "required_media_refs": [],
        "continuity_resolutions": ["Connect Fig 3D to the discussion opening."],
        "meta": {"retrieval_rounds": 1},
    }
    paper = SimpleNamespace(
        url="https://example.com/paper",
        doi="10.1000/demo",
        arxiv_url="https://arxiv.org/abs/1234.5678",
        pdf_url="https://example.com/paper.pdf",
    )

    resource_bundle, authored_plan = literature_api._build_page_artifact_v2_authored_plan_from_session(
        paper=paper,
        compose_payload=_build_sample_compose_payload_for_dossier_v2(),
        reading_dossier=dossier,
        session_payload=session,
    )

    assert authored_plan["external_resources"][0]["url"] == "https://example.com/usmle"
    assert resource_bundle["meta"]["retrieval_rounds"] == 1


def test_page_artifact_v2_authored_plan_should_preserve_reader_opening_and_adjacent_bridges():
    dossier = _build_sample_reading_dossier_v2_for_session()
    session = literature_api._build_experience_session_v2(
        cache_key="lit:experience_session:v2:test",
        reading_dossier=dossier,
        focus_page=7,
        reader_profile="curious_generalist",
        max_iterations=4,
        max_tool_rounds=6,
        narrative_brief=_build_sample_experience_session_v2_narrative_brief(),
    )
    session["meta"]["latest_artifact_draft"] = _build_sample_experience_session_v2_artifact_draft(
        nodes=[
            {
                "node_kind": "heading",
                "text": "先抓 Fig 3 的比较对象。",
            },
            {
                "node_kind": "paragraph",
                "text": "本页先建立 concordance 与 DOI 这两个主指标的阅读顺序。",
            },
            {
                "node_kind": "original_excerpt",
                "display_text": "We first examined the frequency of insight.",
                "source_layout_ids": ["layout:7:1"],
                "source_block_ids": ["blk-7-1"],
            },
            {
                "node_kind": "paragraph",
                "text": "接着用短 excerpt 把正文和图证绑在一起。",
            },
        ],
    )
    session["meta"]["latest_resource_bundle"] = literature_api._build_reader_v2_seed_resource_bundle(
        paper=SimpleNamespace(
            url="https://example.com/paper",
            doi="10.1000/demo",
            arxiv_url="https://arxiv.org/abs/1234.5678",
            pdf_url="https://example.com/paper.pdf",
        ),
        compose_payload=_build_sample_compose_payload_for_dossier_v2(),
        narrative_brief=session["meta"]["latest_narrative_brief"],
    )

    _resource_bundle, authored_plan = literature_api._build_page_artifact_v2_authored_plan_from_session(
        paper=SimpleNamespace(url="", doi="", arxiv_url="", pdf_url=""),
        compose_payload=_build_sample_compose_payload_for_dossier_v2(),
        reading_dossier=dossier,
        session_payload=session,
    )

    assert authored_plan["meta"]["reader_opening"]["key_points"][0].startswith("本页先用 Fig 3")
    assert authored_plan["meta"]["reader_opening"]["previous_page_bridge"]["page"] == 6
    assert authored_plan["meta"]["reader_opening"]["previous_page_preview"]["page"] == 6
    assert authored_plan["meta"]["reader_opening"]["previous_page_preview"]["summary"] == "Previous page sets up the figure comparison."
    assert any(
        "Figure 焦点：Figure 2" in item
        for item in authored_plan["meta"]["reader_opening"]["previous_page_preview"]["key_points"]
    )
    assert authored_plan["meta"]["reader_outro"]["next_page_bridge"]["page"] == 8
    assert authored_plan["meta"]["reader_outro"]["next_page_preview"]["page"] == 8
    assert authored_plan["meta"]["reader_outro"]["next_page_preview"]["summary"] == "Next page expands the score table."
    assert any(
        "Table 焦点：Table 2" in item
        for item in authored_plan["meta"]["reader_outro"]["next_page_preview"]["key_points"]
    )


def test_page_artifact_v2_should_follow_promoted_draft_node_sequence_order():
    dossier = _build_sample_reading_dossier_v2_for_session()
    session = literature_api._build_experience_session_v2(
        cache_key="lit:experience_session:v2:test",
        reading_dossier=dossier,
        focus_page=7,
        reader_profile="curious_generalist",
        max_iterations=4,
        max_tool_rounds=6,
        narrative_brief=_build_sample_experience_session_v2_narrative_brief(),
    )
    session["meta"]["latest_artifact_draft"] = _build_sample_experience_session_v2_artifact_draft(
        nodes=[
            {
                "node_kind": "heading",
                "text": "先抓住 Figure 3 的比较对象。",
                "group_id": "g-intro",
                "group_label": "Figure-first entry",
            },
            {
                "node_kind": "original_excerpt",
                "display_text": "We first examined the frequency of insight.",
                "source_layout_ids": ["layout:7:1"],
                "source_block_ids": ["blk-7-1"],
                "group_id": "g-intro",
                "placement": "inline",
            },
            {
                "node_kind": "paragraph",
                "text": "这页先看 concordance 与 DOI，再回到正文解释为什么这两个指标一起判断教育价值。",
                "group_id": "g-intro",
                "placement": "block",
            },
            {
                "node_kind": "figure_slot",
                "label": "Figure 3",
                "caption": "让图先承担当前页主锚点。",
                "source_layout_ids": ["layout:7:fig1"],
                "group_id": "g-intro",
                "prominence": "hero",
            },
        ],
    )
    session["meta"]["latest_resource_bundle"] = literature_api._build_reader_v2_seed_resource_bundle(
        paper=SimpleNamespace(
            url="https://example.com/paper",
            doi="10.1000/demo",
            arxiv_url="https://arxiv.org/abs/1234.5678",
            pdf_url="https://example.com/paper.pdf",
        ),
        compose_payload=_build_sample_compose_payload_for_dossier_v2(),
        narrative_brief=session["meta"]["latest_narrative_brief"],
    )
    paper = SimpleNamespace(
        url="https://example.com/paper",
        doi="10.1000/demo",
        arxiv_url="https://arxiv.org/abs/1234.5678",
        pdf_url="https://example.com/paper.pdf",
    )

    _resource_bundle, authored_plan = literature_api._build_page_artifact_v2_authored_plan_from_session(
        paper=paper,
        compose_payload=_build_sample_compose_payload_for_dossier_v2(),
        reading_dossier=dossier,
        session_payload=session,
    )
    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=dossier,
        authored_plan=authored_plan,
    )

    segment_kinds = [block["segment_kind"] for block in artifact["reading_blocks"][:4]]
    assert segment_kinds == ["heading", "original_excerpt", "paragraph", "figure_slot"]
    assert artifact["reading_blocks"][0]["meta"]["group_id"] == "g-intro"
    assert artifact["reading_blocks"][1]["meta"]["placement"] == "inline"


def test_page_artifact_v2_should_expose_reader_opening_and_outro_meta():
    dossier = _build_sample_reading_dossier_v2_for_session()
    authored_plan = literature_api.PageArtifactV2AuthoredPlanInput.model_validate(
        {
            "template_id": "guided_mixed_media_v1",
            "layout_recipe": "current_page_spine_interleave_v1",
            "presentation_mode": "mixed_layout",
            "widget_family": "reader_v2_surface",
            "motion_preset": "calm_progressive",
            "interaction_policy": "reader_first_guided",
            "authored_text_blocks": [
                {
                    "segment_kind": "heading",
                    "text": "先抓 Fig 3 的总体比较。",
                    "meta": {"group_id": "g-1"},
                },
                {
                    "segment_kind": "paragraph",
                    "text": "这页要先把 concordance 和 DOI 的两条指标线并排看清。",
                    "meta": {"group_id": "g-1"},
                },
            ],
            "excerpt_overrides": [
                {
                    "display_text": "We first examined the frequency of insight.",
                    "source_layout_ids": ["layout:7:1"],
                    "source_block_ids": ["blk-7-1"],
                }
            ],
            "meta": {
                "reader_opening": {
                    "summary": "本页先抓 Fig 3，再回到正文解释 DOI。",
                    "key_points": ["先看图里的总体比较。", "再读 DOI 对教育价值意味着什么。"],
                    "previous_page_bridge": {
                        "page": 6,
                        "key_points": ["上一页先铺了准确率和判定框架。"],
                        "bridge_text": "本页沿着上一页的问题，继续问回答是否真的具有教学价值。",
                    },
                },
                "reader_outro": {
                    "next_page_bridge": {
                        "page": 8,
                        "key_points": ["下一页会把 DOI 带进 discussion。"],
                        "bridge_text": "读完本页图证后，下一页会把这些结果转成讨论层解释。",
                    }
                },
            },
        }
    ).model_dump(mode="json")

    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=dossier,
        authored_plan=authored_plan,
    )

    assert artifact["meta"]["reader_opening"]["summary"].startswith("本页先抓 Fig 3")
    assert artifact["meta"]["reader_opening"]["previous_page_bridge"]["page"] == 6
    assert artifact["meta"]["reader_outro"]["next_page_bridge"]["page"] == 8


def test_page_artifact_v2_should_preserve_excerpt_translation_from_draft():
    dossier = _build_sample_reading_dossier_v2_for_session()
    session = literature_api._build_experience_session_v2(
        cache_key="lit:experience_session:v2:test",
        reading_dossier=dossier,
        focus_page=7,
        reader_profile="curious_generalist",
        max_iterations=4,
        max_tool_rounds=6,
        narrative_brief=_build_sample_experience_session_v2_narrative_brief(),
    )
    session["meta"]["latest_artifact_draft"] = _build_sample_experience_session_v2_artifact_draft(
        nodes=[
            {
                "node_kind": "original_excerpt",
                "display_text": "We first examined the frequency of insight.",
                "translation_zh": "我们首先考察了洞见出现的频率。",
                "source_layout_ids": ["layout:7:1"],
                "source_block_ids": ["blk-7-1"],
            },
            {
                "node_kind": "paragraph",
                "text": "先让读者看到原文，再用中文讲清它在这一页里的功能。",
            },
        ],
    )
    session["meta"]["latest_resource_bundle"] = literature_api._build_reader_v2_seed_resource_bundle(
        paper=SimpleNamespace(
            url="https://example.com/paper",
            doi="10.1000/demo",
            arxiv_url="https://arxiv.org/abs/1234.5678",
            pdf_url="https://example.com/paper.pdf",
        ),
        compose_payload=_build_sample_compose_payload_for_dossier_v2(),
        narrative_brief=session["meta"]["latest_narrative_brief"],
    )
    paper = SimpleNamespace(
        url="https://example.com/paper",
        doi="10.1000/demo",
        arxiv_url="https://arxiv.org/abs/1234.5678",
        pdf_url="https://example.com/paper.pdf",
    )

    _resource_bundle, authored_plan = literature_api._build_page_artifact_v2_authored_plan_from_session(
        paper=paper,
        compose_payload=_build_sample_compose_payload_for_dossier_v2(),
        reading_dossier=dossier,
        session_payload=session,
    )
    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=dossier,
        authored_plan=authored_plan,
    )

    excerpt_block = next(
        block for block in artifact["reading_blocks"] if block["segment_kind"] == "original_excerpt"
    )
    assert excerpt_block["meta"]["translation_zh"] == "我们首先考察了洞见出现的频率。"


def test_page_artifact_v2_should_resolve_multiple_excerpt_subranges_from_same_grounding_row():
    dossier = _build_sample_reading_dossier_v2_for_session()
    long_text = (
        "We first examined the frequency (prevalence) of insight. Overall, ChatGPT produced at least one significant "
        "insight in 88.9% of all responses. Insight frequency was generally consistent between exam type and question "
        "input format (Fig 3C). In Step 2CK however, insight decreased by 10.3% (n = 11 items) between MC-NJ and MC-J "
        "formulations, paralleling the decrement in accuracy (Fig 1B). Review of this subset of questions did not "
        "reveal a discernible pattern. Next, we quantified the density of insight (DOI) contained within AI-generated "
        "explanations. A density index was defined by normalizing the number of unique insights against the number of "
        "possible answer choices. This analysis was performed on MC-J entries only. High quality outputs were generally "
        "characterized by DOI > 0.6."
    )
    dossier["current_page"]["rich_grounding"]["reading_nodes"] = [
        {
            "node_id": "layout:7:long",
            "node_kind": "paragraph",
            "clean_text": long_text,
            "normalized_text": long_text,
            "raw_text": long_text,
            "source_layout_ids": ["layout:7:long"],
            "source_block_ids": [f"blk-{idx}" for idx in range(1, 11)],
            "include_in_main_flow": True,
            "meta": {"layout_type": "text"},
        }
    ]
    dossier["current_page"]["rich_grounding"]["layout_atoms"] = []
    dossier["current_page"]["rich_grounding"]["evidence_map"] = []

    artifact_draft = _build_sample_experience_session_v2_artifact_draft(
        nodes=[
            {
                "node_kind": "heading",
                "text": "先确认 insight prevalence，再进入 DOI。",
                "group_id": "g-long",
            },
            {
                "node_kind": "original_excerpt",
                "display_text": "We first examined the frequency (prevalence) of insight. Overall, ChatGPT produced at least one significant insight in 88.9% of all responses.",
                "source_layout_ids": ["layout:7:long"],
                "source_block_ids": [f"blk-{idx}" for idx in range(1, 7)],
                "group_id": "g-long",
                "placement": "inline",
            },
            {
                "node_kind": "paragraph",
                "text": "先让读者看到 insight 的总体频率，再解释它为什么和图 3C 对齐。",
                "group_id": "g-long",
            },
            {
                "node_kind": "original_excerpt",
                "display_text": "Next, we quantified the density of insight (DOI) contained within AI-generated explanations. A density index was defined by normalizing the number of unique insights against the number of possible answer choices.",
                "source_layout_ids": ["layout:7:long"],
                "source_block_ids": [f"blk-{idx}" for idx in range(7, 11)],
                "group_id": "g-long",
                "placement": "inline",
            },
            {
                "node_kind": "paragraph",
                "text": "接着再把 DOI 作为第二段主线，而不是把整段原文一次性倒给读者。",
                "group_id": "g-long",
            },
        ],
    )
    authored_plan = literature_api._promote_experience_v2_artifact_draft_to_authored_plan(
        artifact_draft=artifact_draft,
        resource_bundle={"bundle_entries": [], "external_resources": [], "required_media_refs": [], "continuity_resolutions": [], "meta": {}},
    )

    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=dossier,
        authored_plan=authored_plan,
    )

    excerpt_blocks = [block for block in artifact["reading_blocks"] if block["segment_kind"] == "original_excerpt"]
    assert len(excerpt_blocks) == 2
    assert excerpt_blocks[0]["source_block_ids"] == [f"blk-{idx}" for idx in range(1, 7)]
    assert excerpt_blocks[1]["source_block_ids"] == [f"blk-{idx}" for idx in range(7, 11)]
    assert excerpt_blocks[1]["text"].startswith("Next, we quantified the density of insight")


def test_page_artifact_v2_should_allow_draft_selected_media_caption_excerpt():
    dossier = _build_sample_reading_dossier_v2_for_session()
    dossier["current_page"]["rich_grounding"]["reading_nodes"] = []
    dossier["current_page"]["rich_grounding"]["layout_atoms"] = [
        {
            "layout_id": "layout:7:figcap",
            "layout_type": "figure",
            "clean_text": "Fig 3. Concordance and insight of ChatGPT on USMLE. Overall concordance across all exam types and question encoding formats.",
            "normalized_text": "Fig 3. Concordance and insight of ChatGPT on USMLE. Overall concordance across all exam types and question encoding formats.",
            "raw_text": "Fig 3. Concordance and insight of ChatGPT on USMLE. Overall concordance across all exam types and question encoding formats.",
            "canonical_block_ids": ["figcap-1", "figcap-2", "figcap-3"],
            "include_in_main_flow": True,
        }
    ]
    dossier["current_page"]["rich_grounding"]["evidence_map"] = []

    artifact_draft = _build_sample_experience_session_v2_artifact_draft(
        nodes=[
            {
                "node_kind": "heading",
                "text": "先用图注建立 Figure 3 的阅读框架。",
            },
            {
                "node_kind": "original_excerpt",
                "display_text": "Fig 3. Concordance and insight of ChatGPT on USMLE.",
                "source_layout_ids": ["layout:7:figcap"],
                "source_block_ids": ["figcap-1", "figcap-2", "figcap-3"],
            },
            {
                "node_kind": "paragraph",
                "text": "图注本身就说明了这一页的比较对象，所以可以作为导读锚点。",
            },
        ],
    )
    authored_plan = literature_api._promote_experience_v2_artifact_draft_to_authored_plan(
        artifact_draft=artifact_draft,
        resource_bundle={"bundle_entries": [], "external_resources": [], "required_media_refs": [], "continuity_resolutions": [], "meta": {}},
    )

    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=dossier,
        authored_plan=authored_plan,
    )

    excerpt_blocks = [block for block in artifact["reading_blocks"] if block["segment_kind"] == "original_excerpt"]
    assert len(excerpt_blocks) == 1
    assert excerpt_blocks[0]["source_layout_ids"] == ["layout:7:figcap"]
    assert excerpt_blocks[0]["text"].startswith("Fig 3. Concordance and insight")


def test_page_artifact_v2_should_allow_draft_selected_figure_name_excerpt():
    dossier = _build_sample_reading_dossier_v2_for_session()
    dossier["current_page"]["rich_grounding"]["reading_nodes"] = []
    dossier["current_page"]["rich_grounding"]["layout_atoms"] = [
        {
            "layout_id": "layout:7:figure-name",
            "layout_type": "figure_name",
            "clean_text": "Fig 3. Concordance and insight of ChatGPT on USMLE. Overall concordance across all exam types and question encoding formats.",
            "normalized_text": "Fig 3. Concordance and insight of ChatGPT on USMLE. Overall concordance across all exam types and question encoding formats.",
            "raw_text": "Fig 3. Concordance and insight of ChatGPT on USMLE. Overall concordance across all exam types and question encoding formats.",
            "canonical_block_ids": ["fig-name-1", "fig-name-2", "fig-name-3"],
            "include_in_main_flow": True,
        }
    ]
    dossier["current_page"]["rich_grounding"]["evidence_map"] = []

    artifact_draft = _build_sample_experience_session_v2_artifact_draft(
        nodes=[
            {
                "node_kind": "heading",
                "text": "先用 Figure 3 图题建立阅读框架。",
            },
            {
                "node_kind": "original_excerpt",
                "display_text": "Fig 3. Concordance and insight of ChatGPT on USMLE.",
                "source_layout_ids": ["layout:7:figure-name"],
                "source_block_ids": ["fig-name-1", "fig-name-2", "fig-name-3"],
            },
            {
                "node_kind": "paragraph",
                "text": "figure_name 这类图题也应该允许进入当前页 excerpt 解析，而不是被当成 OCR 垃圾丢掉。",
            },
        ],
    )
    authored_plan = literature_api._promote_experience_v2_artifact_draft_to_authored_plan(
        artifact_draft=artifact_draft,
        resource_bundle={"bundle_entries": [], "external_resources": [], "required_media_refs": [], "continuity_resolutions": [], "meta": {}},
    )

    artifact = literature_api._build_page_artifact_v2_from_dossier(
        reading_dossier=dossier,
        authored_plan=authored_plan,
    )

    excerpt_blocks = [block for block in artifact["reading_blocks"] if block["segment_kind"] == "original_excerpt"]
    assert len(excerpt_blocks) == 1
    assert excerpt_blocks[0]["source_layout_ids"] == ["layout:7:figure-name"]
    assert excerpt_blocks[0]["text"].startswith("Fig 3. Concordance and insight")


def test_page_artifact_v2_compact_source_context_should_include_figure_name_excerpt_candidates():
    dossier = _build_sample_reading_dossier_v2_for_session()
    dossier["current_page"]["rich_grounding"]["reading_nodes"] = []
    dossier["current_page"]["rich_grounding"]["layout_atoms"] = [
        {
            "layout_id": "layout:7:figure-name",
            "layout_type": "figure_name",
            "clean_text": "Fig 3. Concordance and insight of ChatGPT on USMLE. Overall concordance across all exam types and question encoding formats.",
            "normalized_text": "Fig 3. Concordance and insight of ChatGPT on USMLE. Overall concordance across all exam types and question encoding formats.",
            "raw_text": "Fig 3. Concordance and insight of ChatGPT on USMLE. Overall concordance across all exam types and question encoding formats.",
            "canonical_block_ids": ["fig-name-1", "fig-name-2", "fig-name-3"],
            "include_in_main_flow": True,
        }
    ]

    compact = literature_api._build_page_artifact_v2_compact_source_context(
        reading_dossier=dossier,
        focus_page=7,
    )

    candidates = list(compact.get("excerpt_candidates") or [])
    assert any(
        candidate.get("source_layout_ids") == ["layout:7:figure-name"]
        and str(candidate.get("display_text") or "").startswith("Fig 3. Concordance and insight")
        for candidate in candidates
    )


def test_experience_session_v2_control_trace_fixture_should_not_contain_legacy_adjacent_markers():
    fixture = json.loads(
        Path("docs/plan/fixtures/experience_session_v2_control_trace_p78_p7.json").read_text(encoding="utf-8")
    )
    blob = json.dumps(fixture, ensure_ascii=False)

    assert "legacy_phase1_fixture" not in blob
    assert "normalized_from" not in blob
    for iteration in list(fixture.get("iterations") or []):
        brief = literature_api._jsonable_dict(iteration.get("narrative_brief") or {})
        continuity_rows = [str(item).strip() for item in list(brief.get("continuity_resolutions") or []) if str(item).strip()]
        assert continuity_rows
        assert all('"content_stream"' not in row for row in continuity_rows)
        assert all('"body_text"' not in row for row in continuity_rows)
        assert all('"summary"' not in row for row in continuity_rows)


def test_experience_session_v2_should_reject_full_adjacent_payload_replay_in_revise_turn():
    dossier = _build_sample_reading_dossier_v2_for_session()
    session = literature_api._build_experience_session_v2(
        cache_key="lit:experience_session:v2:test",
        reading_dossier=dossier,
        focus_page=7,
        reader_profile="curious_generalist",
        max_iterations=4,
        max_tool_rounds=6,
        narrative_brief=_build_sample_experience_session_v2_narrative_brief(),
    )

    with pytest.raises(ValueError, match="cannot replay full neighboring-page structured payload"):
        literature_api._append_experience_session_v2_iteration(
            session,
            phase="revise",
            delta_packet={"adjacent_pages": dossier["adjacent_pages"]},
            state_handle="sess:iter:2",
        )


def test_experience_session_v2_failed_state_should_be_explicit_and_resumable():
    dossier = _build_sample_reading_dossier_v2_for_session()
    session = literature_api._build_experience_session_v2(
        cache_key="lit:experience_session:v2:test",
        reading_dossier=dossier,
        focus_page=7,
        reader_profile="curious_generalist",
        max_iterations=4,
        max_tool_rounds=6,
        narrative_brief=_build_sample_experience_session_v2_narrative_brief(),
    )
    failed = literature_api._mark_experience_session_v2_failed(
        session,
        stop_reason="tool_round_budget_exceeded",
        resume_state_handle="sess:iter:1",
    )

    assert failed["status"] == "failed"
    assert failed["stop_reason"] == "tool_round_budget_exceeded"
    assert failed["resume"]["resumable"] is True
    assert failed["resume"]["preferred_strategy"] == "resume"
    assert failed["resume"]["resume_state_handle"] == "sess:iter:1"
    assert failed["resume"]["resume_token"]


def test_experience_session_v2_should_block_second_full_generation_pass_after_completed_artifact():
    dossier = _build_sample_reading_dossier_v2_for_session()
    session = literature_api._build_experience_session_v2(
        cache_key="lit:experience_session:v2:test",
        reading_dossier=dossier,
        focus_page=7,
        reader_profile="curious_generalist",
        max_iterations=4,
        max_tool_rounds=6,
        narrative_brief=_build_sample_experience_session_v2_narrative_brief(),
    )
    session["artifact_promotion"]["completed_artifact_exists"] = True

    with pytest.raises(ValueError, match="second full-generation pass is blocked"):
        literature_api._append_experience_session_v2_iteration(
            session,
            phase="revise",
            delta_packet={"updated_sections": ["s1"]},
            state_handle="sess:iter:2",
        )


def test_experience_session_v2_should_block_iteration_limit_at_helper_layer():
    dossier = _build_sample_reading_dossier_v2_for_session()
    session = literature_api._build_experience_session_v2(
        cache_key="lit:experience_session:v2:test",
        reading_dossier=dossier,
        focus_page=7,
        reader_profile="curious_generalist",
        max_iterations=1,
        max_tool_rounds=6,
        narrative_brief=_build_sample_experience_session_v2_narrative_brief(),
    )

    with pytest.raises(ValueError, match="max_iterations exceeded"):
        literature_api._append_experience_session_v2_iteration(
            session,
            phase="revise",
            delta_packet={"updated_sections": ["s1"]},
            state_handle="sess:iter:2",
        )


def test_experience_session_v2_should_block_tool_round_limit_at_helper_layer():
    dossier = _build_sample_reading_dossier_v2_for_session()
    session = literature_api._build_experience_session_v2(
        cache_key="lit:experience_session:v2:test",
        reading_dossier=dossier,
        focus_page=7,
        reader_profile="curious_generalist",
        max_iterations=4,
        max_tool_rounds=1,
        narrative_brief=_build_sample_experience_session_v2_narrative_brief(),
    )

    with pytest.raises(ValueError, match="max_tool_rounds exceeded"):
        literature_api._append_experience_session_v2_iteration(
            session,
            phase="revise",
            delta_packet={"updated_sections": ["s1"]},
            state_handle="sess:iter:2",
            tool_trace=[
                {"round_index": 1, "tool_name": "knowledge_search", "arguments": {"query": "alpha"}},
                {"round_index": 2, "tool_name": "knowledge_search", "arguments": {"query": "beta"}},
            ],
        )


def test_experience_session_v2_should_block_duplicate_tool_call_for_same_session_path():
    dossier = _build_sample_reading_dossier_v2_for_session()
    session = literature_api._build_experience_session_v2(
        cache_key="lit:experience_session:v2:test",
        reading_dossier=dossier,
        focus_page=7,
        reader_profile="curious_generalist",
        max_iterations=4,
        max_tool_rounds=6,
        narrative_brief=_build_sample_experience_session_v2_narrative_brief(),
    )
    once = literature_api._append_experience_session_v2_iteration(
        session,
        phase="revise",
        delta_packet={"updated_sections": ["s1"]},
        state_handle="sess:iter:2",
        tool_trace=[{"round_index": 1, "tool_name": "knowledge_search", "arguments": {"query": "same"}}],
    )

    with pytest.raises(ValueError, match="duplicate tool call blocked"):
        literature_api._append_experience_session_v2_iteration(
            once,
            phase="revise",
            delta_packet={"updated_sections": ["s2"]},
            state_handle="sess:iter:3",
            tool_trace=[{"round_index": 1, "tool_name": "knowledge_search", "arguments": {"query": "same"}}],
        )


def test_experience_session_v2_should_allow_non_duplicate_tool_calls():
    dossier = _build_sample_reading_dossier_v2_for_session()
    session = literature_api._build_experience_session_v2(
        cache_key="lit:experience_session:v2:test",
        reading_dossier=dossier,
        focus_page=7,
        reader_profile="curious_generalist",
        max_iterations=4,
        max_tool_rounds=6,
        narrative_brief=_build_sample_experience_session_v2_narrative_brief(),
    )
    once = literature_api._append_experience_session_v2_iteration(
        session,
        phase="revise",
        delta_packet={"updated_sections": ["s1"]},
        state_handle="sess:iter:2",
        tool_trace=[{"round_index": 1, "tool_name": "knowledge_search", "arguments": {"query": "same"}}],
    )
    twice = literature_api._append_experience_session_v2_iteration(
        once,
        phase="revise",
        delta_packet={"updated_sections": ["s2"]},
        state_handle="sess:iter:3",
        tool_trace=[{"round_index": 1, "tool_name": "knowledge_search", "arguments": {"query": "different"}}],
    )

    assert len(twice["iterations"]) == 3
    assert twice["iterations"][2]["tool_trace"][0]["meta"]["tool_arguments"]["query"] == "different"


@pytest.mark.asyncio
async def test_experience_session_v2_cache_set_get_should_use_dedicated_kind_and_namespace(monkeypatch):
    literature_api._experience_session_v2_cache_memory.clear()
    dossier = _build_sample_reading_dossier_v2_for_session()
    dossier_sig = literature_api._reading_dossier_v2_signature(dossier)
    cache_key = literature_api._experience_session_v2_cache_key(
        user_id=5,
        paper_id=78,
        focus_page=7,
        selected_kb_id=0,
        dossier_signature=dossier_sig,
        user_intent="build experience",
        reader_profile="curious_generalist",
    )
    payload = {"status": "running", "session_id": "s-v2"}
    db_writes: list[dict[str, object]] = []
    expires_at = datetime.now() + timedelta(seconds=120)

    class _FakeRedis:
        async def get(self, _cache_key):
            return None

        async def set(self, _key, _value, ex):
            assert ex > 0

    async def _fake_get_redis_client():
        return _FakeRedis()

    async def _fake_plan_cache_db_set(cache_key, plan_kind, payload, **kwargs):
        db_writes.append({"cache_key": cache_key, "plan_kind": plan_kind, "payload": payload, **kwargs})

    async def _fake_plan_cache_db_get(cache_key, plan_kind):
        assert cache_key.startswith(f"{literature_api.EXPERIENCE_SESSION_V2_CACHE_NAMESPACE}:")
        assert plan_kind == literature_api.EXPERIENCE_SESSION_V2_CACHE_KIND
        return payload, expires_at

    monkeypatch.setattr(literature_api, "_get_redis_client", _fake_get_redis_client)
    monkeypatch.setattr(literature_api, "_plan_cache_db_set", _fake_plan_cache_db_set)
    monkeypatch.setattr(literature_api, "_plan_cache_db_get", _fake_plan_cache_db_get)

    await literature_api._experience_session_v2_cache_set(
        cache_key,
        payload,
        user_id=5,
        paper_id=78,
        page=7,
        compose_source_signature="compose-sig",
    )
    literature_api._experience_session_v2_cache_memory.clear()
    result, layer = await literature_api._experience_session_v2_cache_get(cache_key)

    assert db_writes and db_writes[0]["cache_key"] == cache_key
    assert db_writes[0]["plan_kind"] == literature_api.EXPERIENCE_SESSION_V2_CACHE_KIND
    assert result == payload
    assert layer == "db"


@pytest.mark.asyncio
async def test_reader_experience_v2_cached_payload_should_return_generation_shell_when_no_completed_artifact_exists(monkeypatch):
    paper = SimpleNamespace(id=78, user_id=5, title="Demo Paper", url="https://example.com/paper", pdf_path="demo.pdf")
    compose_payload = _build_sample_compose_payload_for_dossier_v2()

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    async def _fake_adjacent(**_kwargs):
        return _build_sample_adjacent_structured_context_for_dossier_v2()

    async def _fake_session_get(_cache_key):
        return None, "none"

    async def _fake_artifact_get(_cache_key):
        return None, "none"

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _make_fake_v2_compose_service(compose_payload))
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_structured_context_v2", _fake_adjacent)
    monkeypatch.setattr(literature_api, "_experience_session_v2_cache_get", _fake_session_get)
    monkeypatch.setattr(literature_api, "_page_artifact_v2_cache_get", _fake_artifact_get)

    payload = literature_api.ReaderExperiencePlanRequest(page=7, focus_page=7, reader_profile="curious_generalist")
    response = await literature_api._build_reader_experience_v2_cached_payload(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert response["status"] == "generating"
    assert response["artifact"] is None
    assert response["compose_source_signature"] == "compose-sig"


@pytest.mark.asyncio
async def test_prepare_reader_experience_v2_runtime_should_fail_loudly_when_structured_adjacent_context_is_unavailable(monkeypatch):
    paper = SimpleNamespace(id=78, user_id=5, title="Demo Paper", url="https://example.com/paper", pdf_path="demo.pdf")
    compose_payload = _build_sample_compose_payload_for_dossier_v2()

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    async def _fake_adjacent(**_kwargs):
        raise ValueError("neighboring-page structured context unavailable for v2 route")

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _make_fake_v2_compose_service(compose_payload))
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_structured_context_v2", _fake_adjacent)

    with pytest.raises(HTTPException, match="neighboring-page structured context unavailable for v2 route"):
        await literature_api._prepare_reader_experience_v2_runtime(
            paper_id=78,
            payload=literature_api.ReaderExperiencePlanRequest(page=7, focus_page=7, reader_profile="curious_generalist"),
            db=SimpleNamespace(),
            current_user=SimpleNamespace(id=5),
        )


@pytest.mark.asyncio
async def test_reader_experience_v2_live_payload_should_build_and_persist_completed_artifact(monkeypatch):
    paper = SimpleNamespace(
        id=78,
        user_id=5,
        title="Demo Paper",
        url="https://example.com/paper",
        doi="10.1000/demo",
        arxiv_url="https://arxiv.org/abs/1234.5678",
        pdf_path="demo.pdf",
        pdf_url="https://example.com/paper.pdf",
    )
    compose_payload = _build_sample_compose_payload_for_dossier_v2()
    captured: dict[str, object] = {}

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    async def _fake_adjacent(**_kwargs):
        return _build_sample_adjacent_structured_context_for_dossier_v2()

    async def _fake_session_get(_cache_key):
        return None, "none"

    async def _fake_artifact_get(_cache_key):
        return None, "none"

    async def _fake_session_set(cache_key, payload, **_kwargs):
        captured["session_cache_key"] = cache_key
        captured["session_payload"] = payload

    async def _fake_artifact_set(cache_key, payload, **_kwargs):
        captured["artifact_cache_key"] = cache_key
        captured["artifact_payload"] = payload

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _make_fake_v2_compose_service(compose_payload))
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_structured_context_v2", _fake_adjacent)
    monkeypatch.setattr(literature_api, "_experience_session_v2_cache_get", _fake_session_get)
    monkeypatch.setattr(literature_api, "_page_artifact_v2_cache_get", _fake_artifact_get)
    monkeypatch.setattr(literature_api, "_experience_session_v2_cache_set", _fake_session_set)
    monkeypatch.setattr(literature_api, "_page_artifact_v2_cache_set", _fake_artifact_set)
    _patch_fake_experience_session_v2_narrative_brief_generator(monkeypatch)
    _patch_fake_experience_session_v2_artifact_draft_generator(monkeypatch)

    payload = literature_api.ReaderExperiencePlanRequest(
        page=7,
        focus_page=7,
        reader_profile="curious_generalist",
        user_intent="build v2 experience",
    )
    response = await literature_api._build_reader_experience_v2_payload(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert response["status"] == "ready"
    assert response["artifact"]["version"] == "page_artifact_v2"
    assert captured["artifact_cache_key"].startswith(f"{literature_api.PAGE_ARTIFACT_V2_CACHE_NAMESPACE}:")
    assert captured["session_cache_key"].startswith(f"{literature_api.EXPERIENCE_SESSION_V2_CACHE_NAMESPACE}:")
    assert captured["artifact_cache_key"] != captured["session_cache_key"]
    v1_experience_key = literature_api._experience_plan_cache_key(
        user_id=5,
        paper_id=78,
        focus_page=7,
        selected_kb_id=0,
        compose_source_signature="compose-sig",
        generative_plan_signature="gen-plan-sig",
        user_intent="build v2 experience",
        reader_profile="curious_generalist",
        focus_section_ids=[],
    )
    assert captured["artifact_cache_key"] != v1_experience_key
    assert not captured["artifact_cache_key"].startswith("lit:experience:")
    reader_visible_text = " ".join(
        str(block.get("text") or "")
        for block in list(response["artifact"]["reading_blocks"] or [])
        if str(block.get("segment_kind") or "") in {"authored_explanation", "aside_content", "original_excerpt"}
    )
    assert "previous_page:" not in reader_visible_text
    assert "next_page:" not in reader_visible_text
    assert "吸收到当前页解释里" not in reader_visible_text
    session_payload = captured["session_payload"]
    assert session_payload["status"] == "completed"
    assert session_payload["artifact_promotion"]["completed_artifact_exists"] is True
    assert session_payload["iterations"][0]["narrative_brief"]["meta"]["generator_mode"] == "model_generated_bootstrap"
    assert session_payload["meta"]["latest_artifact_draft"]["meta"]["generator_mode"] == "model_generated_artifact_draft"
    assert len(session_payload["iterations"]) == 2
    revise = session_payload["iterations"][1]
    assert revise["context_carry"]["mode"] == "delta_state_handle"
    assert "adjacent_pages" not in revise["context_carry"]["delta_packet"]
    assert "resource_bundle" in revise["context_carry"]["delta_packet"]["working_state"]


@pytest.mark.asyncio
async def test_reader_experience_v2_live_payload_should_fail_loudly_when_model_brief_generation_fails(monkeypatch):
    paper = SimpleNamespace(id=78, user_id=5, title="Demo Paper", url="https://example.com/paper", pdf_path="demo.pdf")
    compose_payload = _build_sample_compose_payload_for_dossier_v2()

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    async def _fake_adjacent(**_kwargs):
        return _build_sample_adjacent_structured_context_for_dossier_v2()

    async def _fake_session_get(_cache_key):
        return None, "none"

    async def _fake_artifact_get(_cache_key):
        return None, "none"

    async def _fake_generate(**_kwargs):
        raise ValueError("narrative brief generation failed: invalid JSON output")

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _make_fake_v2_compose_service(compose_payload))
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_structured_context_v2", _fake_adjacent)
    monkeypatch.setattr(literature_api, "_experience_session_v2_cache_get", _fake_session_get)
    monkeypatch.setattr(literature_api, "_page_artifact_v2_cache_get", _fake_artifact_get)
    monkeypatch.setattr(literature_api, "_generate_experience_session_v2_narrative_brief", _fake_generate)

    with pytest.raises(HTTPException, match="narrative brief generation failed: invalid JSON output"):
        await literature_api._build_reader_experience_v2_payload(
            paper_id=78,
            payload=literature_api.ReaderExperiencePlanRequest(page=7, focus_page=7, reader_profile="curious_generalist"),
            db=SimpleNamespace(),
            current_user=SimpleNamespace(id=5),
        )


@pytest.mark.asyncio
async def test_reader_experience_v2_live_payload_should_surface_failed_session_without_v1_fallback(monkeypatch):
    paper = SimpleNamespace(id=78, user_id=5, title="Demo Paper", url="https://example.com/paper", pdf_path="demo.pdf")
    compose_payload = _build_sample_compose_payload_for_dossier_v2()
    failed_session = literature_api._build_experience_session_v2(
        cache_key="lit:experience_session:v2:test",
        reading_dossier=_build_sample_reading_dossier_v2_for_session(),
        focus_page=7,
        reader_profile="curious_generalist",
        max_iterations=4,
        max_tool_rounds=4,
        narrative_brief=_build_sample_experience_session_v2_narrative_brief(),
    )
    failed_session = literature_api._mark_experience_session_v2_failed(
        failed_session,
        stop_reason="narrative brief generation failed",
        resume_state_handle="iter:1:bootstrap",
    )

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    async def _fake_adjacent(**_kwargs):
        return _build_sample_adjacent_structured_context_for_dossier_v2()

    async def _fake_session_get(_cache_key):
        return failed_session, "db"

    async def _fake_artifact_get(_cache_key):
        return None, "none"

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _make_fake_v2_compose_service(compose_payload))
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_structured_context_v2", _fake_adjacent)
    monkeypatch.setattr(literature_api, "_experience_session_v2_cache_get", _fake_session_get)
    monkeypatch.setattr(literature_api, "_page_artifact_v2_cache_get", _fake_artifact_get)

    payload = literature_api.ReaderExperiencePlanRequest(page=7, focus_page=7, reader_profile="curious_generalist")
    cached_response = await literature_api._build_reader_experience_v2_cached_payload(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )
    assert cached_response["status"] == "failed"
    assert cached_response["failure_detail"] == "narrative brief generation failed"

    async def _fake_session_set(_cache_key, _payload, **_kwargs):
        return None

    async def _fake_artifact_set(_cache_key, _payload, **_kwargs):
        return None

    monkeypatch.setattr(literature_api, "_experience_session_v2_cache_set", _fake_session_set)
    monkeypatch.setattr(literature_api, "_page_artifact_v2_cache_set", _fake_artifact_set)
    _patch_fake_experience_session_v2_narrative_brief_generator(monkeypatch)
    _patch_fake_experience_session_v2_artifact_draft_generator(monkeypatch)

    response = await literature_api._build_reader_experience_v2_payload(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )
    assert response["status"] == "ready"
    assert response["artifact"]["version"] == "page_artifact_v2"


@pytest.mark.asyncio
async def test_reader_workbench_v2_payload_should_expose_dossier_session_artifact_and_failure_visibility(monkeypatch):
    paper = SimpleNamespace(
        id=78,
        user_id=5,
        title="Demo Paper",
        url="https://example.com/paper",
        pdf_path="demo.pdf",
        pdf_url="https://example.com/paper.pdf",
    )
    compose_payload = _build_sample_compose_payload_for_dossier_v2()

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    async def _fake_adjacent(**_kwargs):
        return _build_sample_adjacent_structured_context_for_dossier_v2()

    async def _fake_session_get(_cache_key):
        return None, "none"

    async def _fake_artifact_get(_cache_key):
        return None, "none"

    async def _fake_session_set(_cache_key, _payload, **_kwargs):
        return None

    async def _fake_artifact_set(_cache_key, _payload, **_kwargs):
        return None

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _make_fake_v2_compose_service(compose_payload))
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_structured_context_v2", _fake_adjacent)
    monkeypatch.setattr(literature_api, "_experience_session_v2_cache_get", _fake_session_get)
    monkeypatch.setattr(literature_api, "_page_artifact_v2_cache_get", _fake_artifact_get)
    monkeypatch.setattr(literature_api, "_experience_session_v2_cache_set", _fake_session_set)
    monkeypatch.setattr(literature_api, "_page_artifact_v2_cache_set", _fake_artifact_set)
    _patch_fake_experience_session_v2_narrative_brief_generator(monkeypatch)
    _patch_fake_experience_session_v2_artifact_draft_generator(monkeypatch)

    payload = literature_api.ReaderExperiencePlanRequest(page=7, focus_page=7, reader_profile="curious_generalist")
    response = await literature_api._build_reader_workbench_v2_payload(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert response["reading_dossier"]["version"] == "reading_dossier_v2"
    assert response["session"]["version"] == "experience_session_v2"
    assert response["session"]["iterations"][0]["narrative_brief"]["current_page_main_arc"]
    assert response["artifact"]["version"] == "page_artifact_v2"
    assert response["artifact_validation"]["valid"] is True
    assert response["meta"]["presentation_rationale"]


@pytest.mark.asyncio
async def test_reader_workbench_v2_payload_should_surface_failed_state_explicitly(monkeypatch):
    paper = SimpleNamespace(id=78, user_id=5, title="Demo Paper", url="https://example.com/paper", pdf_path="demo.pdf")
    compose_payload = _build_sample_compose_payload_for_dossier_v2()
    failed_session = literature_api._build_experience_session_v2(
        cache_key="lit:experience_session:v2:test",
        reading_dossier=_build_sample_reading_dossier_v2_for_session(),
        focus_page=7,
        reader_profile="curious_generalist",
        max_iterations=4,
        max_tool_rounds=4,
        narrative_brief=_build_sample_experience_session_v2_narrative_brief(),
    )
    failed_session = literature_api._mark_experience_session_v2_failed(
        failed_session,
        stop_reason="media/resource binding unresolved",
        resume_state_handle="iter:2:artifact-draft",
    )

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    async def _fake_adjacent(**_kwargs):
        return _build_sample_adjacent_structured_context_for_dossier_v2()

    async def _fake_session_get(_cache_key):
        return failed_session, "db"

    async def _fake_artifact_get(_cache_key):
        return None, "none"

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _make_fake_v2_compose_service(compose_payload))
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_structured_context_v2", _fake_adjacent)
    monkeypatch.setattr(literature_api, "_experience_session_v2_cache_get", _fake_session_get)
    monkeypatch.setattr(literature_api, "_page_artifact_v2_cache_get", _fake_artifact_get)

    response = await literature_api._build_reader_workbench_v2_payload(
        paper_id=78,
        payload=literature_api.ReaderExperiencePlanRequest(page=7, focus_page=7, reader_profile="curious_generalist"),
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert response["status"] == "failed"
    assert response["failure_detail"] == "media/resource binding unresolved"
    assert response["reading_dossier"]["version"] == "reading_dossier_v2"
    assert response["session"]["status"] == "failed"
    assert response["session"]["iterations"][0]["narrative_brief"]["current_page_main_arc"]
    assert response["artifact"] is None
