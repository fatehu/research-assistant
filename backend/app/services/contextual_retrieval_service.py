"""Utilities for contextual retrieval and adjacent-window enrichment."""

from __future__ import annotations

from typing import Any, Mapping


def normalize_adjacent_window(window: int | None) -> int:
    """Clamp adjacent window size to [1, 3]."""
    if window is None:
        return 1
    return max(1, min(int(window), 3))


def build_context_summary(
    document_name: str | None,
    chunk_level: str | None = None,
    section_title: str | None = None,
    section_type: str | None = None,
    metadata: dict[str, Any] | None = None,
    max_length: int = 240,
) -> str:
    """Build lightweight structural context string for a chunk."""
    parts: list[str] = []

    doc_name = (document_name or "").strip()
    if doc_name:
        parts.append(f"文档:{doc_name}")

    level = (chunk_level or "").strip()
    if level:
        parts.append(f"层级:{level}")

    title = (section_title or "").strip()
    if title:
        parts.append(f"章节:{title}")
    else:
        sec_type = (section_type or "").strip()
        if sec_type:
            parts.append(f"章节类型:{sec_type}")

    path_text = ""
    if isinstance(metadata, dict):
        path_raw = metadata.get("hierarchy_path") or metadata.get("path")
        if isinstance(path_raw, list):
            nodes = [str(node).strip() for node in path_raw if str(node).strip()]
            if nodes:
                path_text = " > ".join(nodes)
        elif isinstance(path_raw, str):
            path_text = path_raw.strip()

    if path_text:
        parts.append(f"路径:{path_text}")

    summary = " | ".join(parts).strip()
    if not summary:
        summary = "文档:unknown"

    if len(summary) <= max_length:
        return summary
    return summary[: max(0, max_length - 3)] + "..."


def compose_embedding_input(
    content: str,
    context_summary: str | None,
    chunk_level: str | None,
) -> str:
    """Compose embedding text; paragraph chunks prepend structural context."""
    body = (content or "").strip()
    if not body:
        return ""

    level = (chunk_level or "").strip().lower()
    summary = (context_summary or "").strip()
    if level != "paragraph" or not summary:
        return body

    return f"[Context]\n{summary}\n[Content]\n{body}"


def build_reranker_input(
    *,
    content: str,
    context_summary: str | None = None,
    document_name: str | None = None,
    section_title: str | None = None,
    section_type: str | None = None,
    max_context_length: int = 220,
    max_content_length: int = 960,
) -> str:
    """Compose a compact structured input for cross-encoder reranking."""
    body = (content or "").strip()
    if not body:
        return ""

    context = (context_summary or "").strip()
    if not context:
        context = build_context_summary(
            document_name=document_name,
            section_title=section_title,
            section_type=section_type,
            max_length=max_context_length,
        )
    elif len(context) > max_context_length:
        context = context[: max(0, max_context_length - 3)] + "..."

    snippet = body
    if len(snippet) > max_content_length:
        snippet = snippet[: max(0, max_content_length - 3)] + "..."

    return f"[Context]\n{context}\n[Content]\n{snippet}"


def build_adjacent_lookup_keys(document_id: int, chunk_index: int, window: int) -> list[tuple[int, int]]:
    """Build (document_id, chunk_index) keys for adjacent context lookup."""
    normalized_window = normalize_adjacent_window(window)
    doc_id = int(document_id)
    idx = int(chunk_index)
    keys: list[tuple[int, int]] = []

    for offset in range(-normalized_window, normalized_window + 1):
        if offset == 0:
            continue
        target_idx = idx + offset
        if target_idx < 0:
            continue
        keys.append((doc_id, target_idx))
    return keys


def merge_adjacent_context(
    document_id: int,
    chunk_index: int,
    window: int,
    row_map: Mapping[tuple[int, int], Any],
    content_limit: int = 260,
) -> list[dict[str, Any]]:
    """Collect adjacent chunk payloads from pre-fetched row map."""
    doc_id = int(document_id)
    idx = int(chunk_index)
    normalized_window = normalize_adjacent_window(window)
    results: list[dict[str, Any]] = []

    for offset in range(-normalized_window, normalized_window + 1):
        if offset == 0:
            continue
        target_idx = idx + offset
        if target_idx < 0:
            continue

        row = row_map.get((doc_id, target_idx))
        if row is None:
            continue

        content = (getattr(row, "content", "") or "").strip()
        if len(content) > content_limit:
            content = content[:content_limit] + "..."

        results.append(
            {
                "chunk_id": int(getattr(row, "id")),
                "chunk_index": int(getattr(row, "chunk_index")),
                "relative_offset": int(offset),
                "chunk_level": getattr(row, "chunk_level", None),
                "section_title": getattr(row, "section_title", None),
                "content": content,
            }
        )

    return results
