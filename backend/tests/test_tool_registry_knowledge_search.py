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

    assert "knowledge_search" in with_db._tools
    assert "knowledge_search" not in without_db._tools
    assert "knowledge_search" in with_factory._tools


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
    state = SimpleNamespace()

    async def _rewrite(query):
        call_order.append("rewrite")
        return SimpleNamespace(enabled=True, vector_variants=[1], text_variants=[1])

    async def _retrieve(db, query, rewrite_result, runtime):
        call_order.append("retrieve")
        return state

    async def _rerank(query, payload):
        call_order.append("rerank")
        return [("c1", None)]

    async def _compress(**kwargs):
        call_order.append("compress")
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
    )

    assert result.success is True
    assert call_order == ["rewrite", "retrieve", "rerank", "compress"]


@pytest.mark.asyncio
async def test_knowledge_search_execute_with_db_returns_guided_reading_payload(monkeypatch):
    tool = agent_tools.KnowledgeSearchTool(db=None, user_id=1)
    state = SimpleNamespace()

    async def _rewrite(query):
        return SimpleNamespace(enabled=True, vector_variants=[1], text_variants=[1])

    async def _retrieve(db, query, rewrite_result, runtime):
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


def test_api_search_filters_embedding_dimension():
    knowledge_api = (
        Path(__file__).resolve().parents[1] / "app" / "api" / "knowledge.py"
    ).read_text(encoding="utf-8")
    assert "dc.embedding_dimension = :vector_dimension" in knowledge_api
    assert "\"vector_dimension\": group_dimension" in knowledge_api
    assert "embedding::vector(" in knowledge_api
