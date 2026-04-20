import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.contextual_retrieval_service import (
    build_adjacent_lookup_keys,
    build_context_summary,
    build_reranker_input,
    compose_embedding_input,
    merge_adjacent_context,
    normalize_adjacent_window,
)


@dataclass
class _Row:
    id: int
    document_id: int
    chunk_index: int
    chunk_level: str
    section_title: str | None
    content: str


def test_normalize_adjacent_window_bounds():
    assert normalize_adjacent_window(0) == 1
    assert normalize_adjacent_window(1) == 1
    assert normalize_adjacent_window(3) == 3
    assert normalize_adjacent_window(10) == 3


def test_build_context_summary_contains_structural_fields():
    summary = build_context_summary(
        document_name="paper.pdf",
        chunk_level="paragraph",
        section_title="Method",
        section_type="methodology",
        metadata={"hierarchy_path": ["1", "1.2", "Method"]},
    )
    assert "文档:paper.pdf" in summary
    assert "层级:paragraph" in summary
    assert "章节:Method" in summary
    assert "路径:1 > 1.2 > Method" in summary


def test_compose_embedding_input_for_paragraph_and_non_paragraph():
    paragraph_text = compose_embedding_input(
        content="This is content",
        context_summary="文档:paper | 层级:paragraph",
        chunk_level="paragraph",
    )
    assert "[Context]" in paragraph_text
    assert "[Content]" in paragraph_text

    section_text = compose_embedding_input(
        content="Section content",
        context_summary="文档:paper | 层级:section",
        chunk_level="section",
    )
    assert section_text == "Section content"


def test_build_reranker_input_uses_structural_context_and_trims_body():
    reranker_input = build_reranker_input(
        content="A" * 120,
        context_summary="文档:paper.pdf | 章节:Method",
        max_context_length=40,
        max_content_length=60,
    )

    assert "[Context]" in reranker_input
    assert "[Content]" in reranker_input
    assert "文档:paper.pdf" in reranker_input
    assert len(reranker_input.split("[Content]\n", 1)[1]) <= 60


def test_build_reranker_input_falls_back_to_document_and_section_metadata():
    reranker_input = build_reranker_input(
        content="This is the body.",
        document_name="demo.pdf",
        section_title="Results",
        section_type="results",
        context_summary=None,
    )

    assert "demo.pdf" in reranker_input
    assert "Results" in reranker_input


def test_build_adjacent_lookup_keys_respects_boundary():
    keys = build_adjacent_lookup_keys(document_id=10, chunk_index=0, window=2)
    assert keys == [(10, 1), (10, 2)]


def test_merge_adjacent_context_returns_ordered_window_items():
    row_map = {
        (10, 3): _Row(1003, 10, 3, "paragraph", "Intro", "A" * 40),
        (10, 5): _Row(1005, 10, 5, "paragraph", "Intro", "B" * 40),
    }
    merged = merge_adjacent_context(document_id=10, chunk_index=4, window=1, row_map=row_map, content_limit=30)

    assert len(merged) == 2
    assert merged[0]["chunk_index"] == 3
    assert merged[0]["relative_offset"] == -1
    assert merged[1]["chunk_index"] == 5
    assert merged[1]["relative_offset"] == 1
    assert merged[0]["content"].endswith("...")
