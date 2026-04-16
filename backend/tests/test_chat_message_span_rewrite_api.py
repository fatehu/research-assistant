import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api import chat as chat_api


def test_resolve_message_span_offsets_uses_occurrence_index():
    content = "alpha beta alpha beta"

    start, end = chat_api._resolve_message_span_offsets(
        content,
        "alpha",
        occurrence_index=1,
    )

    assert content[start:end] == "alpha"
    assert start == content.rfind("alpha")


def test_resolve_message_span_offsets_disambiguates_with_context():
    content = "引言：模型很好。结论：模型很好。"

    start, end = chat_api._resolve_message_span_offsets(
        content,
        "模型很好",
        before_context="结论：",
        after_context="。",
    )

    assert content[start:end] == "模型很好"
    assert start == content.rfind("模型很好")


def test_resolve_message_span_offsets_rejects_ambiguous_span():
    with pytest.raises(HTTPException) as exc_info:
        chat_api._resolve_message_span_offsets("同一段。同一段。", "同一段")

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "selected_span_ambiguous"


def test_validate_span_rewrite_replacement_preserves_existing_citations():
    replacement = chat_api._validate_span_rewrite_replacement(
        selected_text="原始事实[网页1]",
        replacement_text="更清楚的原始事实[网页1]",
    )

    assert replacement == "更清楚的原始事实[网页1]"


def test_validate_span_rewrite_replacement_rejects_added_citation_label():
    with pytest.raises(HTTPException) as exc_info:
        chat_api._validate_span_rewrite_replacement(
            selected_text="原始事实",
            replacement_text="原始事实[网页1]",
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["code"] == "rewrite_added_citation_label"


def test_validate_span_rewrite_replacement_rejects_dropped_citation_label():
    with pytest.raises(HTTPException) as exc_info:
        chat_api._validate_span_rewrite_replacement(
            selected_text="原始事实[网页1]",
            replacement_text="原始事实",
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["code"] == "rewrite_dropped_citation_label"


def test_validate_span_rewrite_replacement_allows_expanded_line_structure():
    replacement = chat_api._validate_span_rewrite_replacement(
        selected_text="技术实现侧重工具调用",
        replacement_text="技术实现侧重工具调用。\n它还包括任务规划、工具选择与结果整合。",
    )

    assert replacement == "技术实现侧重工具调用。\n它还包括任务规划、工具选择与结果整合。"


def test_validate_span_rewrite_replacement_rejects_added_heading_structure():
    with pytest.raises(HTTPException) as exc_info:
        chat_api._validate_span_rewrite_replacement(
            selected_text="技术实现侧重工具调用",
            replacement_text="## 技术实现侧重工具调用",
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["code"] == "rewrite_added_markdown_heading"


def test_preserve_span_rewrite_markdown_scaffold_keeps_numbering_and_bold_labels():
    replacement = chat_api._preserve_span_rewrite_markdown_scaffold(
        selected_text=(
            "1. **架构设计理念不同**\n"
            "- **传统 RAG**：采用检索生成流程[来源1]\n"
            "- **Agentic Search**：采用代理架构[来源1]"
        ),
        replacement_text=(
            "架构理念不同\n"
            "传统 RAG：采用检索生成流程[来源1]\n"
            "Agentic Search：采用代理架构[来源1]"
        ),
    )

    assert replacement.splitlines() == [
        "1. **架构理念不同**",
        "- **传统 RAG**：采用检索生成流程[来源1]",
        "- **Agentic Search**：采用代理架构[来源1]",
    ]


def test_preserve_span_rewrite_markdown_scaffold_replaces_existing_prefix():
    replacement = chat_api._preserve_span_rewrite_markdown_scaffold(
        selected_text="- **传统 RAG**：原句[来源1]",
        replacement_text="1. 传统 RAG：新句[来源1]",
    )

    assert replacement == "- **传统 RAG**：新句[来源1]"


def test_preserve_span_rewrite_markdown_scaffold_does_not_add_heading_to_plain_text():
    replacement = chat_api._preserve_span_rewrite_markdown_scaffold(
        selected_text="技术实现侧重工具调用",
        replacement_text="## 技术实现侧重工具调用",
    )

    assert replacement == "技术实现侧重工具调用"


def test_preserve_span_rewrite_markdown_scaffold_normalizes_heading_level():
    replacement = chat_api._preserve_span_rewrite_markdown_scaffold(
        selected_text="### 1. **技术实现**",
        replacement_text="## 技术实现",
    )

    assert replacement == "### 1. **技术实现**"
