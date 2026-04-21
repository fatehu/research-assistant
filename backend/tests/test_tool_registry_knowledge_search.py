import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services import agent_tools
from app.services.query_rewrite_service import QueryRewriteResult, QueryVariant


def _fake_tool(name: str):
    return SimpleNamespace(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
    )


def test_tool_registry_registers_knowledge_search_only_when_db_available(monkeypatch):
    monkeypatch.setattr(
        agent_tools,
        "KnowledgeSearchTool",
        lambda db, user_id, db_session_factory=None: _fake_tool("knowledge_search"),
    )
    monkeypatch.setattr(agent_tools, "WebSearchTool", lambda: _fake_tool("web_search"))
    monkeypatch.setattr(agent_tools, "CalculatorTool", lambda: _fake_tool("calculator"))
    monkeypatch.setattr(agent_tools, "DateTimeTool", lambda: _fake_tool("datetime"))
    monkeypatch.setattr(agent_tools, "TextAnalysisTool", lambda: _fake_tool("text_analysis"))
    monkeypatch.setattr(agent_tools, "UnitConverterTool", lambda: _fake_tool("unit_converter"))
    monkeypatch.setattr(agent_tools, "LiteratureSearchTool", lambda: _fake_tool("literature_search"))

    with_db = agent_tools.ToolRegistry(db=object(), user_id=1)
    without_db = agent_tools.ToolRegistry(db=None, user_id=1)
    with_factory = agent_tools.ToolRegistry(db=None, db_session_factory=lambda: object(), user_id=1)
    codelab = agent_tools.ToolRegistry(db=object(), user_id=1, route_profile="codelab")

    paper_tool_names = {
        "paper_research_get_artifact_manifest",
        "paper_research_prepare",
        "paper_research_read_artifact",
        "paper_research_read_implementation_spec",
        "paper_research_read_run_drafts",
        "paper_research_read_repo_file",
        "paper_research_search_repo",
        "paper_research_status",
        "paper_research_write_execution_script",
        "paper_research_write_implementation_spec",
        "paper_research_write_run_drafts",
        "paper_research_create_run_draft",
    }

    assert "knowledge_search" in with_db._tools
    assert paper_tool_names.issubset(set(with_db._tools))
    assert "knowledge_search" not in without_db._tools
    assert paper_tool_names.isdisjoint(set(without_db._tools))
    assert "knowledge_search" in with_factory._tools
    assert paper_tool_names.issubset(set(with_factory._tools))
    assert "knowledge_search" in codelab._tools
    assert paper_tool_names.isdisjoint(set(codelab._tools))


def test_paper_research_workspace_missing_required_archives(tmp_path):
    tool = agent_tools.PaperResearchStatusTool(db=None, user_id=1)
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True)
    assert tool._workspace_missing_required_archives(workspace_dir) is True
    (workspace_dir / "paper_intake_result.json").write_text("{}", encoding="utf-8")
    (workspace_dir / "experiment_spec.json").write_text("{}", encoding="utf-8")
    (workspace_dir / "workspace_adapter_manifest.json").write_text("{}", encoding="utf-8")
    assert tool._workspace_missing_required_archives(workspace_dir) is False


def test_paper_research_search_repo_python_fallback_finds_matches(tmp_path):
    repo_dir = tmp_path / "paper_repo"
    repo_dir.mkdir(parents=True)
    (repo_dir / "train.py").write_text(
        "parser.add_argument('--lr', type=float, default=1e-3)\n"
        "parser.add_argument('--epochs', type=int, default=5)\n",
        encoding="utf-8",
    )
    (repo_dir / "README.md").write_text("learning rate is configured in train.py\n", encoding="utf-8")

    payload = agent_tools.PaperResearchSearchRepoTool._search_with_python_fallback(
        repo_dir=repo_dir,
        repo_files=["README.md", "train.py"],
        query="lr",
        max_results=10,
        case_sensitive=False,
        is_regex=False,
        glob="*.py",
    )

    assert payload["engine"] == "python_fallback"
    assert payload["returned_matches"] == 1
    assert payload["matches"][0]["relative_path"] == "repo/source/train.py"
    assert payload["matches"][0]["line_number"] == 1


def test_knowledge_search_runtime_uses_configurable_threshold(monkeypatch):
    monkeypatch.setattr(agent_tools.settings, "enable_reranker", True)
    monkeypatch.setattr(agent_tools.settings, "enable_hybrid_retrieval", True)
    monkeypatch.setattr(agent_tools.settings, "agent_knowledge_score_threshold", 0.65)
    monkeypatch.setattr(agent_tools.settings, "reranker_top_k", 7)
    monkeypatch.setattr(agent_tools.settings, "hybrid_vector_top_k", 9)
    monkeypatch.setattr(agent_tools.settings, "hybrid_text_top_k", 11)

    tool = agent_tools.KnowledgeSearchTool(db=None, user_id=1)
    runtime = tool._resolve_retrieve_runtime(top_k=5)

    assert runtime.distance_threshold == pytest.approx(0.35)
    assert runtime.reranker_candidate_k == 7
    assert runtime.vector_top_k == 9
    assert runtime.text_top_k == 11


def test_knowledge_search_runtime_can_override_reranker_and_hybrid(monkeypatch):
    monkeypatch.setattr(agent_tools.settings, "enable_reranker", True)
    monkeypatch.setattr(agent_tools.settings, "enable_hybrid_retrieval", True)
    tool = agent_tools.KnowledgeSearchTool(db=None, user_id=1)

    runtime = tool._resolve_retrieve_runtime(top_k=4, use_reranker=False, use_hybrid=False)

    assert runtime.use_reranker is False
    assert runtime.use_hybrid is False
    assert runtime.text_top_k == 0


@pytest.mark.asyncio
async def test_knowledge_search_vector_retrieve_passes_dimension_and_threshold(monkeypatch):
    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _FakeDB:
        def __init__(self):
            self.calls = []

        async def execute(self, query, params=None):
            self.calls.append(params or {})
            if len(self.calls) == 1:
                return _Result(
                    [SimpleNamespace(embedding_model="m", embedding_dimension=3, chunk_count=8)]
                )
            return _Result(
                [
                    SimpleNamespace(
                        id=11,
                        similarity=0.88,
                        document_id=2,
                        knowledge_base_id=5,
                        content="hit",
                        chunk_index=0,
                        embedding_model="m",
                        embedding_dimension=3,
                        text_score=None,
                        document_name="doc.md",
                        knowledge_base_name="kb",
                    )
                ]
            )

    class _EmbedSvc:
        async def embed_texts(self, texts, is_query=False):
            return [[0.1, 0.2, 0.3] for _ in texts]

        async def embed_text(self, text, is_query=False):
            return [0.1, 0.2, 0.3]

    async def _noop_apply(*args, **kwargs):
        return None

    monkeypatch.setattr(agent_tools, "get_embedding_service_for_model_and_dimension", lambda *args, **kwargs: _EmbedSvc())
    monkeypatch.setattr(agent_tools, "apply_hnsw_ef_search", _noop_apply)
    monkeypatch.setattr(agent_tools.settings, "pgvector_hnsw_ef_search", 40)

    tool = agent_tools.KnowledgeSearchTool(db=None, user_id=1)
    runtime = tool._resolve_retrieve_runtime(top_k=5)
    rewrite_result = QueryRewriteResult(
        original_query="q",
        enabled=True,
        strategies=["original"],
        synonym_queries=[],
        sub_queries=[],
        hyde_document=None,
        vector_variants=[QueryVariant(text="q", strategy="original")],
        text_variants=[QueryVariant(text="q", strategy="original")],
    )
    db = _FakeDB()

    await tool._retrieve_vector_rows(
        db=db,
        query="q",
        rewrite_result=rewrite_result,
        kb_ids=[1],
        document_ids=[],
        runtime=runtime,
    )

    assert len(db.calls) >= 2
    vector_call = db.calls[1]
    assert vector_call["vector_dimension"] == 3
    assert vector_call["distance_threshold"] == pytest.approx(runtime.distance_threshold)
    assert "query_vector" in vector_call


@pytest.mark.asyncio
async def test_knowledge_search_execute_with_db_uses_pipeline_steps(monkeypatch):
    tool = agent_tools.KnowledgeSearchTool(db=None, user_id=1)
    call_order = []
    state = SimpleNamespace(resolved_kb_ids=set(), resolved_document_ids=set())
    captured = {}

    async def _rewrite(query, *, use_query_rewrite=None):
        call_order.append("rewrite")
        return SimpleNamespace(enabled=True, vector_variants=[1], text_variants=[1])

    async def _retrieve(db, query, rewrite_result, runtime, **kwargs):
        call_order.append("retrieve")
        return state

    async def _rerank(query, payload):
        call_order.append("rerank")
        return [("c1", None)]

    async def _compress(**kwargs):
        call_order.append("compress")
        captured["use_contextual_compression"] = kwargs.get("use_contextual_compression")
        return [{"content": "ok", "score": 1.0, "knowledge_base": "kb", "document": "doc"}]

    monkeypatch.setattr(tool, "_rewrite", _rewrite)
    monkeypatch.setattr(tool, "_retrieve", _retrieve)
    monkeypatch.setattr(tool, "_rerank", _rerank)
    monkeypatch.setattr(tool, "_compress", _compress)
    monkeypatch.setattr(tool, "_format_retrieval_output", lambda *args, **kwargs: "ok")
    monkeypatch.setattr(tool, "_log_retrieval_metrics", lambda *args, **kwargs: None)

    result = await tool._execute_with_db(
        db=SimpleNamespace(),
        query="test",
        top_k=3,
        include_adjacent_chunks=False,
        adjacent_window=1,
        use_contextual_compression=False,
    )

    assert result.success is True
    assert call_order == ["rewrite", "retrieve", "rerank", "compress"]
    assert captured["use_contextual_compression"] is False


@pytest.mark.asyncio
async def test_knowledge_search_execute_with_db_returns_guided_reading_payload(monkeypatch):
    tool = agent_tools.KnowledgeSearchTool(db=None, user_id=1)
    state = SimpleNamespace(resolved_kb_ids=set(), resolved_document_ids=set())

    async def _rewrite(query, *, use_query_rewrite=None):
        return SimpleNamespace(enabled=True, vector_variants=[1], text_variants=[1])

    async def _retrieve(db, query, rewrite_result, runtime, **kwargs):
        return state

    async def _rerank(query, payload):
        return [("candidate", None)]

    async def _compress(**kwargs):
        return [
            {
                "content": "Figure 3 shows the strongest improvement on the reasoning benchmark.",
                "score": 0.91,
                "knowledge_base": "Exam KB",
                "document": "demo-paper.pdf",
                "chunk_index": 4,
                "retrieval_mode": "hybrid",
            }
        ]

    monkeypatch.setattr(tool, "_rewrite", _rewrite)
    monkeypatch.setattr(tool, "_retrieve", _retrieve)
    monkeypatch.setattr(tool, "_rerank", _rerank)
    monkeypatch.setattr(tool, "_compress", _compress)
    monkeypatch.setattr(tool, "_log_retrieval_metrics", lambda *args, **kwargs: None)

    result = await tool._execute_with_db(
        db=SimpleNamespace(),
        query="figure 3 benchmark improvement",
        top_k=3,
        include_adjacent_chunks=False,
        adjacent_window=1,
    )

    assert result.success is True
    assert "知识库线索" in result.output
    assert result.data["source_kind"] == "knowledge_base_search"
    assert result.data["reader_summary"]
    assert result.data["results"][0]["source_label"] == "Exam KB / demo-paper.pdf"
    assert result.data["structured_content"]["results"][0]["rank"] == 1
    assert result.data["structured_content"]["knowledge_base_hits"][0]["knowledge_base"] == "Exam KB"
    assert result.data["provenance"]["tool_kind"] == "knowledge_search"


@pytest.mark.asyncio
async def test_knowledge_search_rewrite_can_be_disabled(monkeypatch):
    tool = agent_tools.KnowledgeSearchTool(db=None, user_id=1)
    captured = {}

    async def _fake_rewrite_query(query, *, rewrite_mode="auto", use_query_rewrite=True):
        captured["query"] = query
        captured["use_query_rewrite"] = use_query_rewrite
        return QueryRewriteResult(
            original_query=query,
            enabled=bool(use_query_rewrite),
            strategies=["original"],
            synonym_queries=[],
            sub_queries=[],
            hyde_document=None,
            vector_variants=[QueryVariant(text=query, strategy="original")],
            text_variants=[QueryVariant(text=query, strategy="original")],
        )

    monkeypatch.setattr(tool.query_rewrite_service, "rewrite_query", _fake_rewrite_query)

    result = await tool._rewrite("attention", use_query_rewrite=False)

    assert result.enabled is False
    assert captured["use_query_rewrite"] is False


@pytest.mark.asyncio
async def test_knowledge_search_rewrite_can_pass_profile(monkeypatch):
    tool = agent_tools.KnowledgeSearchTool(db=None, user_id=1)
    captured = {}

    async def _fake_rewrite_query(query, *, rewrite_mode="auto", use_query_rewrite=True, rewrite_profile=None):
        captured["query"] = query
        captured["use_query_rewrite"] = use_query_rewrite
        captured["rewrite_profile"] = rewrite_profile
        return QueryRewriteResult(
            original_query=query,
            enabled=bool(use_query_rewrite),
            strategies=["synonym"],
            synonym_queries=[],
            sub_queries=[],
            hyde_document=None,
            vector_variants=[QueryVariant(text=query, strategy="original")],
            text_variants=[QueryVariant(text=query, strategy="original")],
        )

    monkeypatch.setattr(tool.query_rewrite_service, "rewrite_query", _fake_rewrite_query)

    result = await tool._rewrite("attention", query_rewrite_profile="light")

    assert result.enabled is True
    assert captured["use_query_rewrite"] is True
    assert captured["rewrite_profile"] == "light"


@pytest.mark.asyncio
async def test_knowledge_search_scope_can_resolve_requested_document_ids(monkeypatch):
    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _FakeDB:
        def __init__(self):
            self.calls = 0

        async def execute(self, _stmt):
            self.calls += 1
            if self.calls == 1:
                return _Result([(5,)])
            return _Result([(8, 5), (9, 6)])

    tool = agent_tools.KnowledgeSearchTool(db=None, user_id=1)

    async def _fake_shared_kb_ids(_db):
        return {6}

    monkeypatch.setattr(tool, "_get_shared_kb_ids", _fake_shared_kb_ids)

    kb_ids, document_ids = await tool._resolve_scope(
        _FakeDB(),
        requested_document_ids=[8, 9],
    )

    assert kb_ids == {5, 6}
    assert document_ids == {8, 9}


def test_api_search_filters_embedding_dimension():
    knowledge_api = (
        Path(__file__).resolve().parents[1] / "app" / "api" / "knowledge.py"
    ).read_text(encoding="utf-8")
    assert "dc.embedding_dimension = :vector_dimension" in knowledge_api
    assert "\"vector_dimension\": group_dimension" in knowledge_api
    assert "embedding::vector(" in knowledge_api


def test_paper_research_probe_url_classifies_hdf5_payload():
    tool = agent_tools.PaperResearchStatusTool(db=None, user_id=1)

    ok, downloadable, diagnosis, next_action = tool._probe_url_diagnosis(
        status_code=200,
        content_length=1024,
        detected_kind="hdf5",
        expected_kind="hdf5",
        head_bytes=b"\x89HDF\r\n\x1a\n",
    )

    assert ok is True
    assert downloadable is True
    assert diagnosis == "valid_hdf5"
    assert next_action == "use_as_official_source"


def test_paper_research_probe_url_flags_empty_202_response():
    tool = agent_tools.PaperResearchStatusTool(db=None, user_id=1)

    ok, downloadable, diagnosis, next_action = tool._probe_url_diagnosis(
        status_code=202,
        content_length=0,
        detected_kind="unknown",
        expected_kind="file",
        head_bytes=b"",
    )

    assert ok is False
    assert downloadable is False
    assert diagnosis == "accepted_but_empty"
    assert next_action == "diagnose_official_source_failure"


def test_paper_research_parse_git_ls_remote_extracts_default_branch():
    tool = agent_tools.PaperResearchStatusTool(db=None, user_id=1)

    parsed = tool._parse_git_ls_remote(
        "ref: refs/heads/main\tHEAD\n0123456789abcdef0123456789abcdef01234567\tHEAD\n"
    )

    assert parsed["default_branch"] == "main"
    assert parsed["head_sha"] == "0123456789abcdef0123456789abcdef01234567"
