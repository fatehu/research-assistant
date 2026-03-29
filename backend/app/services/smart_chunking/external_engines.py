from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .token_utils import chars_to_tokens
from .types import ChunkConfig, ResolvedCharLimits, generate_chunk_id


class ThirdPartyEngineUnavailable(RuntimeError):
    """Raised when an optional third-party chunking engine is unavailable."""


@dataclass
class ExternalChunk:
    chunk_id: str
    content: str
    start_char: int
    end_char: int
    header_path: list[str] = field(default_factory=list)
    prev_id: Optional[str] = None
    next_id: Optional[str] = None
    parent_id: Optional[str] = None
    child_ids: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


def split_fixed_with_langchain(
    *,
    text: str,
    limits: ResolvedCharLimits,
) -> list[ExternalChunk]:
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ThirdPartyEngineUnavailable("langchain_text_splitters not installed") from exc

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=max(1, int(limits.base_chunk_size)),
        chunk_overlap=max(0, int(limits.chunk_overlap)),
        add_start_index=True,
    )
    docs = splitter.create_documents([text], metadatas=[{}])
    chunks: list[ExternalChunk] = []
    search_pos = 0
    for doc in docs:
        page_content = str(getattr(doc, "page_content", "") or "")
        content = page_content.strip()
        if not content:
            continue
        metadata = dict(getattr(doc, "metadata", {}) or {})
        start_char = int(metadata.get("start_index", -1))
        if start_char < 0:
            start_char, end_char = _find_text_range(text=text, content=content, search_pos=search_pos)
        else:
            end_char = start_char + len(content)
        search_pos = max(search_pos, start_char)
        chunks.append(
            ExternalChunk(
                chunk_id=generate_chunk_id(content, start_char),
                content=content,
                start_char=start_char,
                end_char=end_char,
                extra={"engine": "langchain", "splitter": "RecursiveCharacterTextSplitter"},
            )
        )
    return chunks


def split_markdown_sections_with_langchain(*, text: str) -> list[ExternalChunk]:
    if not _looks_like_markdown(text):
        return []

    try:
        from langchain_text_splitters import MarkdownHeaderTextSplitter
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ThirdPartyEngineUnavailable("langchain_text_splitters not installed") from exc

    splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[("#", "h1"), ("##", "h2"), ("###", "h3"), ("####", "h4"), ("#####", "h5"), ("######", "h6")],
        strip_headers=False,
    )
    docs = splitter.split_text(text)
    sections: list[ExternalChunk] = []
    search_pos = 0
    for doc in docs:
        content = str(getattr(doc, "page_content", "") or "").strip()
        if not content:
            continue
        start_char, end_char = _find_text_range(text=text, content=content, search_pos=search_pos)
        search_pos = max(search_pos, start_char)
        metadata = dict(getattr(doc, "metadata", {}) or {})
        header_path = _extract_header_path(metadata)
        sections.append(
            ExternalChunk(
                chunk_id=generate_chunk_id(content, start_char),
                content=content,
                start_char=start_char,
                end_char=end_char,
                header_path=header_path,
                extra={
                    "engine": "langchain",
                    "splitter": "MarkdownHeaderTextSplitter",
                    "raw_metadata": metadata,
                },
            )
        )
    return sections


async def split_semantic_with_llamaindex(
    *,
    text: str,
    breakpoint_percentile: float,
    embed_texts_fn: Callable[[list[str]], Awaitable[list[list[float]]]],
    sentence_splitter: Optional[Callable[[str], list[str]]] = None,
    offset: int = 0,
) -> list[ExternalChunk]:
    return await asyncio.to_thread(
        _split_semantic_with_llamaindex_sync,
        text,
        breakpoint_percentile,
        embed_texts_fn,
        sentence_splitter,
        offset,
    )


def split_hierarchical_with_llamaindex(
    *,
    text: str,
    config: ChunkConfig,
    limits: ResolvedCharLimits,
) -> dict[str, list[ExternalChunk]]:
    try:
        from llama_index.core.node_parser.relational.hierarchical import (
            HierarchicalNodeParser,
            get_leaf_nodes,
            get_root_nodes,
        )
        from llama_index.core.schema import Document
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ThirdPartyEngineUnavailable("llama_index.core not installed") from exc

    if config.use_token_based:
        paragraph_tokens = max(32, int(config.base_chunk_tokens))
        section_tokens = max(paragraph_tokens * 2, int(config.max_semantic_tokens))
        document_tokens = max(section_tokens * 2, int(config.max_semantic_tokens) * 3)
    else:
        paragraph_tokens = max(32, chars_to_tokens(int(limits.base_chunk_size), text))
        section_tokens = max(paragraph_tokens * 2, chars_to_tokens(int(limits.max_semantic_chunk), text))
        document_tokens = max(section_tokens * 2, chars_to_tokens(int(limits.max_semantic_chunk * 2), text))

    parser = HierarchicalNodeParser.from_defaults(
        chunk_sizes=[document_tokens, section_tokens, paragraph_tokens],
        include_prev_next_rel=True,
    )
    all_nodes = parser.get_nodes_from_documents([Document(text=text)])
    root_nodes = set(node.node_id for node in get_root_nodes(all_nodes))
    leaf_nodes = set(node.node_id for node in get_leaf_nodes(all_nodes))
    depth_map = _compute_depth_map(all_nodes)

    paragraph_chunks = _map_llama_nodes_to_chunks(
        text=text,
        nodes=[node for node in all_nodes if node.node_id in leaf_nodes],
        level_name="paragraph",
        sequential=True,
    )
    section_chunks = _map_llama_nodes_to_chunks(
        text=text,
        nodes=[
            node
            for node in all_nodes
            if node.node_id not in leaf_nodes and node.node_id not in root_nodes
        ],
        level_name="section",
        sequential=False,
    )
    document_chunks = _map_llama_nodes_to_chunks(
        text=text,
        nodes=[node for node in all_nodes if node.node_id in root_nodes],
        level_name="document",
        sequential=False,
    )

    # Fallback when parser only returns roots+leaves.
    if not section_chunks and document_chunks and paragraph_chunks:
        max_depth = max(depth_map.values(), default=0)
        if max_depth <= 1:
            section_chunks = document_chunks
            document_chunks = []

    return {
        "paragraph": paragraph_chunks,
        "section": section_chunks,
        "document": document_chunks,
    }


def _split_semantic_with_llamaindex_sync(
    text: str,
    breakpoint_percentile: float,
    embed_texts_fn: Callable[[list[str]], Awaitable[list[list[float]]]],
    sentence_splitter: Optional[Callable[[str], list[str]]],
    offset: int,
) -> list[ExternalChunk]:
    try:
        from llama_index.core.base.embeddings.base import BaseEmbedding
        from llama_index.core.bridge.pydantic import Field
        from llama_index.core.node_parser.text.semantic_splitter import SemanticSplitterNodeParser
        from llama_index.core.schema import Document
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ThirdPartyEngineUnavailable("llama_index.core not installed") from exc

    class _EmbeddingAdapter(BaseEmbedding):
        embed_texts_fn: Any = Field(exclude=True)
        model_name: str = "research-assistant"

        @classmethod
        def class_name(cls) -> str:
            return "ResearchAssistantEmbeddingAdapter"

        def _get_query_embedding(self, query: str) -> list[float]:
            return list(asyncio.run(self.embed_texts_fn([query]))[0])

        async def _aget_query_embedding(self, query: str) -> list[float]:
            return list((await self.embed_texts_fn([query]))[0])

        def _get_text_embedding(self, text: str) -> list[float]:
            return list(asyncio.run(self.embed_texts_fn([text]))[0])

        def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
            return [list(item) for item in asyncio.run(self.embed_texts_fn(list(texts)))]

    parser = SemanticSplitterNodeParser.from_defaults(
        embed_model=_EmbeddingAdapter(embed_texts_fn=embed_texts_fn),
        breakpoint_percentile_threshold=max(50, min(99, int(round(breakpoint_percentile)))),
        sentence_splitter=sentence_splitter,
        include_prev_next_rel=True,
    )
    nodes = parser.get_nodes_from_documents([Document(text=text)])
    chunks = _map_llama_nodes_to_chunks(
        text=text,
        nodes=nodes,
        level_name="paragraph",
        sequential=True,
        offset=offset,
    )
    return chunks


def _map_llama_nodes_to_chunks(
    *,
    text: str,
    nodes: list[Any],
    level_name: str,
    sequential: bool,
    offset: int = 0,
) -> list[ExternalChunk]:
    chunks: list[ExternalChunk] = []
    search_pos = 0
    for node in nodes:
        content = str(getattr(node, "text", "") or "").strip()
        if not content:
            continue
        start_char, end_char = _find_text_range(
            text=text,
            content=content,
            search_pos=search_pos if sequential else 0,
        )
        if sequential:
            search_pos = max(search_pos, end_char)
        node_id = str(getattr(node, "node_id", "") or generate_chunk_id(content, start_char + offset))
        prev_node = getattr(node, "prev_node", None)
        next_node = getattr(node, "next_node", None)
        parent_node = getattr(node, "parent_node", None)
        child_nodes = getattr(node, "child_nodes", None) or []
        chunks.append(
            ExternalChunk(
                chunk_id=node_id,
                content=content,
                start_char=start_char + offset,
                end_char=end_char + offset,
                prev_id=getattr(prev_node, "node_id", None),
                next_id=getattr(next_node, "node_id", None),
                parent_id=getattr(parent_node, "node_id", None),
                child_ids=[str(getattr(item, "node_id", None) or getattr(item, "id_", None) or "") for item in list(child_nodes or []) if str(getattr(item, "node_id", None) or getattr(item, "id_", None) or "")],
                extra={
                    "engine": "llamaindex",
                    "splitter": (
                        "SemanticSplitterNodeParser"
                        if level_name == "paragraph"
                        else "HierarchicalNodeParser"
                    ),
                },
            )
        )
    return chunks


def _compute_depth_map(nodes: list[Any]) -> dict[str, int]:
    node_map = {str(getattr(node, "node_id", "") or ""): node for node in nodes}
    cache: dict[str, int] = {}

    def _depth(node_id: str) -> int:
        if node_id in cache:
            return cache[node_id]
        node = node_map.get(node_id)
        if node is None:
            cache[node_id] = 0
            return 0
        parent = getattr(node, "parent_node", None)
        parent_id = str(getattr(parent, "node_id", "") or "")
        if not parent_id:
            cache[node_id] = 0
            return 0
        cache[node_id] = _depth(parent_id) + 1
        return cache[node_id]

    for item in list(node_map.keys()):
        _depth(item)
    return cache


def _find_text_range(*, text: str, content: str, search_pos: int) -> tuple[int, int]:
    idx = text.find(content, max(0, int(search_pos)))
    if idx == -1:
        stripped = content.strip()
        if stripped:
            idx = text.find(stripped, max(0, int(search_pos)))
            if idx != -1:
                return idx, idx + len(stripped)
        idx = max(0, int(search_pos))
        return idx, idx + len(content)
    return idx, idx + len(content)


def _extract_header_path(metadata: dict[str, Any]) -> list[str]:
    if not metadata:
        return []

    def _sort_key(item: tuple[str, Any]) -> tuple[int, str]:
        key = str(item[0] or "")
        match = re.search(r"(\d+)$", key)
        if match:
            return (int(match.group(1)), key)
        return (99, key)

    ordered = []
    for key, value in sorted(metadata.items(), key=_sort_key):
        payload = str(value or "").strip()
        if payload:
            ordered.append(payload)
    return ordered


def _looks_like_markdown(text: str) -> bool:
    return bool(re.search(r"(?m)^\s{0,3}#{1,6}\s+\S", str(text or "")))
