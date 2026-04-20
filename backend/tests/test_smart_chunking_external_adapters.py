import pytest

from app.services.smart_chunking.external_engines import ExternalChunk
from app.services.smart_chunking.service import SmartChunkingService
from app.services.smart_chunking.types import ChunkConfig, ChunkingStrategy


@pytest.mark.asyncio
async def test_fixed_strategy_uses_external_chunk_metadata(monkeypatch):
    def _fake_split_fixed_with_langchain(*, text, limits):
        del text, limits
        return [
            ExternalChunk(
                chunk_id="fixed-1",
                content="## Methods\n\nA stable markdown chunk.",
                start_char=0,
                end_char=36,
                header_path=["Methods"],
                extra={"engine": "langchain", "splitter": "RecursiveCharacterTextSplitter"},
            )
        ]

    monkeypatch.setattr(
        "app.services.smart_chunking.service.split_fixed_with_langchain",
        _fake_split_fixed_with_langchain,
    )

    service = SmartChunkingService()
    result = await service.chunk_document(
        "## Methods\n\nA stable markdown chunk.",
        ChunkConfig(strategy=ChunkingStrategy.FIXED),
        file_type="md",
    )

    assert len(result.chunks) == 1
    chunk = result.chunks[0]
    assert chunk.metadata.extra["engine"] == "langchain"
    assert chunk.metadata.extra["engine_mode"] == "fixed"
    assert chunk.metadata.extra["source_format"] == "md"
    assert chunk.metadata.section_title == "Methods"


@pytest.mark.asyncio
async def test_pdf_markdown_input_is_treated_as_ingest_markdown(monkeypatch):
    captured = {}

    def _fake_preprocess_text(text, file_type="txt", enable_ocr_noise_cleanup=True):
        del enable_ocr_noise_cleanup
        captured["file_type"] = file_type
        return text

    def _fake_split_fixed_with_langchain(*, text, limits):
        del limits
        return [
            ExternalChunk(
                chunk_id="pdf-md-1",
                content=text,
                start_char=0,
                end_char=len(text),
                extra={"engine": "langchain", "splitter": "RecursiveCharacterTextSplitter"},
            )
        ]

    monkeypatch.setattr("app.services.smart_chunking.service.preprocess_text", _fake_preprocess_text)
    monkeypatch.setattr(
        "app.services.smart_chunking.service.split_fixed_with_langchain",
        _fake_split_fixed_with_langchain,
    )

    service = SmartChunkingService()
    result = await service.chunk_document(
        "# Title\n\nBody paragraph.",
        ChunkConfig(strategy=ChunkingStrategy.FIXED),
        file_type="pdf",
    )

    assert captured["file_type"] == "md"
    assert result.chunks[0].metadata.extra["source_format"] == "pdf_ingest_md"


@pytest.mark.asyncio
async def test_academic_strategy_composes_section_and_semantic_split(monkeypatch):
    def _fake_markdown_sections(*, text):
        return [
            ExternalChunk(
                chunk_id="sec-1",
                content=text,
                start_char=0,
                end_char=len(text),
                header_path=["Abstract"],
                extra={"engine": "langchain", "splitter": "MarkdownHeaderTextSplitter"},
            )
        ]

    async def _fake_semantic_split(*, text, breakpoint_percentile, embed_texts_fn, sentence_splitter=None, offset=0):
        del breakpoint_percentile, embed_texts_fn, sentence_splitter
        paragraph = "This study introduces a composed academic chunker."
        start = text.find(paragraph)
        return [
            ExternalChunk(
                chunk_id="para-1",
                content=paragraph,
                start_char=start + offset,
                end_char=start + offset + len(paragraph),
                extra={"engine": "llamaindex", "splitter": "SemanticSplitterNodeParser"},
            )
        ]

    monkeypatch.setattr(
        "app.services.smart_chunking.service.split_markdown_sections_with_langchain",
        _fake_markdown_sections,
    )
    monkeypatch.setattr(
        "app.services.smart_chunking.service.split_semantic_with_llamaindex",
        _fake_semantic_split,
    )

    service = SmartChunkingService()
    text = "# Abstract\n\nThis study introduces a composed academic chunker."
    result = await service.chunk_document(
        text,
        ChunkConfig(strategy=ChunkingStrategy.ACADEMIC, enable_hierarchical=True),
        file_type="md",
    )

    assert result.metadata["is_academic"] is True
    assert result.metadata["engine_mode"] == "academic"
    assert len(result.chunks) == 1
    paragraph = result.chunks[0]
    assert paragraph.metadata.section_title == "Abstract"
    assert paragraph.metadata.section_type == "abstract"
    assert paragraph.metadata.parent_id == "sec-1"
    assert result.hierarchy["section"][0]["metadata"]["child_ids"] == ["para-1"]


@pytest.mark.asyncio
async def test_hierarchical_strategy_maps_external_levels(monkeypatch):
    def _fake_hierarchical(*, text, config, limits):
        del text, config, limits
        return {
            "paragraph": [
                ExternalChunk(
                    chunk_id="p-1",
                    content="Leaf paragraph.",
                    start_char=12,
                    end_char=27,
                    parent_id="s-1",
                    extra={"engine": "llamaindex", "splitter": "HierarchicalNodeParser"},
                )
            ],
            "section": [
                ExternalChunk(
                    chunk_id="s-1",
                    content="## Section\n\nLeaf paragraph.",
                    start_char=0,
                    end_char=27,
                    child_ids=["p-1"],
                    extra={"engine": "llamaindex", "splitter": "HierarchicalNodeParser"},
                )
            ],
            "document": [
                ExternalChunk(
                    chunk_id="d-1",
                    content="# Doc\n\n## Section\n\nLeaf paragraph.",
                    start_char=0,
                    end_char=34,
                    child_ids=["s-1"],
                    extra={"engine": "llamaindex", "splitter": "HierarchicalNodeParser"},
                )
            ],
        }

    monkeypatch.setattr(
        "app.services.smart_chunking.service.split_hierarchical_with_llamaindex",
        _fake_hierarchical,
    )

    service = SmartChunkingService()
    result = await service.chunk_document(
        "# Doc\n\n## Section\n\nLeaf paragraph.",
        ChunkConfig(strategy=ChunkingStrategy.HIERARCHICAL),
        file_type="md",
    )

    assert len(result.chunks) == 1
    assert result.hierarchy["section"][0]["id"] == "s-1"
    assert result.hierarchy["document"][0]["id"] == "d-1"
    assert result.chunks[0].metadata.extra["engine_mode"] == "hierarchical"


@pytest.mark.asyncio
async def test_semantic_strategy_passes_runtime_sentence_splitter(monkeypatch):
    captured = {}

    async def _fake_semantic_split(*, text, breakpoint_percentile, embed_texts_fn, sentence_splitter=None, offset=0):
        del breakpoint_percentile, embed_texts_fn, offset
        captured["sentence_splitter"] = sentence_splitter
        return [
            ExternalChunk(
                chunk_id="semantic-1",
                content=text,
                start_char=0,
                end_char=len(text),
                extra={"engine": "llamaindex", "splitter": "SemanticSplitterNodeParser"},
            )
        ]

    monkeypatch.setattr(
        "app.services.smart_chunking.service.split_semantic_with_llamaindex",
        _fake_semantic_split,
    )

    service = SmartChunkingService()
    text = "Research results improved. Smith (2020) supports the conclusion."
    result = await service.chunk_document(
        text,
        ChunkConfig(strategy=ChunkingStrategy.SEMANTIC, preserve_citations=True, enable_hierarchical=False),
        file_type="md",
    )

    splitter = captured["sentence_splitter"]
    assert splitter is not None
    assert splitter(text) == [text]
    assert result.chunks[0].metadata.extra["engine"] == "llamaindex"


@pytest.mark.asyncio
async def test_semantic_strategy_enforces_external_chunk_limits(monkeypatch):
    async def _fake_semantic_split(*, text, breakpoint_percentile, embed_texts_fn, sentence_splitter=None, offset=0):
        del breakpoint_percentile, embed_texts_fn, sentence_splitter, offset
        return [
            ExternalChunk(
                chunk_id="oversized-1",
                content=text,
                start_char=0,
                end_char=len(text),
                extra={"engine": "llamaindex", "splitter": "SemanticSplitterNodeParser"},
            )
        ]

    monkeypatch.setattr(
        "app.services.smart_chunking.service.split_semantic_with_llamaindex",
        _fake_semantic_split,
    )

    service = SmartChunkingService()
    text = "This is a semantic chunk. " * 160
    result = await service.chunk_document(
        text,
        ChunkConfig(
            strategy=ChunkingStrategy.SEMANTIC,
            use_token_based=True,
            base_chunk_tokens=24,
            overlap_tokens=4,
            min_semantic_tokens=12,
            max_semantic_tokens=64,
            enable_hierarchical=False,
        ),
        file_type="md",
    )

    assert len(result.chunks) > 1
    assert all(chunk.metadata.token_count <= 64 for chunk in result.chunks)
    assert any(
        "split_large_semantic" in chunk.metadata.extra.get("postprocess_steps", [])
        for chunk in result.chunks
    )


@pytest.mark.asyncio
async def test_semantic_strategy_merges_tiny_external_chunks(monkeypatch):
    text = "Short title. This is the explanatory paragraph that should absorb the short title."
    split_pos = text.find("This")

    async def _fake_semantic_split(*, text, breakpoint_percentile, embed_texts_fn, sentence_splitter=None, offset=0):
        del breakpoint_percentile, embed_texts_fn, sentence_splitter
        return [
            ExternalChunk(
                chunk_id="tiny-1",
                content=text[:split_pos].strip(),
                start_char=offset,
                end_char=offset + split_pos,
                extra={"engine": "llamaindex", "splitter": "SemanticSplitterNodeParser"},
            ),
            ExternalChunk(
                chunk_id="tiny-2",
                content=text[split_pos:].strip(),
                start_char=offset + split_pos,
                end_char=offset + len(text),
                extra={"engine": "llamaindex", "splitter": "SemanticSplitterNodeParser"},
            ),
        ]

    monkeypatch.setattr(
        "app.services.smart_chunking.service.split_semantic_with_llamaindex",
        _fake_semantic_split,
    )

    service = SmartChunkingService()
    result = await service.chunk_document(
        text,
        ChunkConfig(
            strategy=ChunkingStrategy.SEMANTIC,
            use_token_based=True,
            min_semantic_tokens=16,
            max_semantic_tokens=96,
            enable_hierarchical=False,
        ),
        file_type="md",
    )

    assert len(result.chunks) == 1
    assert "merge_small_semantic" in result.chunks[0].metadata.extra.get("postprocess_steps", [])


@pytest.mark.asyncio
async def test_hybrid_hierarchy_preserves_semantic_extra_metadata(monkeypatch):
    text = "## Results\n\nSemantic content remains attached to hierarchy."

    async def _fake_semantic_split(*, text, breakpoint_percentile, embed_texts_fn, sentence_splitter=None, offset=0):
        del breakpoint_percentile, embed_texts_fn, sentence_splitter, offset
        start = text.find("Semantic")
        content = text[start:]
        return [
            ExternalChunk(
                chunk_id="semantic-para-1",
                content=content,
                start_char=start,
                end_char=start + len(content),
                header_path=["Results"],
                extra={"engine": "llamaindex", "splitter": "SemanticSplitterNodeParser"},
            )
        ]

    monkeypatch.setattr(
        "app.services.smart_chunking.service.split_semantic_with_llamaindex",
        _fake_semantic_split,
    )

    service = SmartChunkingService()
    result = await service.chunk_document(
        text,
        ChunkConfig(strategy=ChunkingStrategy.HYBRID, enable_hierarchical=True, detect_academic_structure=False),
        file_type="md",
    )

    paragraph = result.hierarchy["paragraph"][0]
    assert paragraph["metadata"]["extra"]["engine"] == "llamaindex"
    assert paragraph["metadata"]["extra"]["engine_mode"] == "semantic"


@pytest.mark.asyncio
async def test_semantic_strategy_skips_semantic_engine_when_budget_precheck_fails(monkeypatch):
    async def _unexpected_semantic_split(*args, **kwargs):
        raise AssertionError("semantic splitter should be skipped when precheck already fails")

    def _fake_fixed_split(*, text, limits):
        del limits
        return [
            ExternalChunk(
                chunk_id="fixed-fallback-1",
                content=text[:40],
                start_char=0,
                end_char=min(len(text), 40),
                extra={"engine": "langchain", "splitter": "RecursiveCharacterTextSplitter"},
            )
        ]

    monkeypatch.setattr(
        "app.services.smart_chunking.service.split_semantic_with_llamaindex",
        _unexpected_semantic_split,
    )
    monkeypatch.setattr(
        "app.services.smart_chunking.service.split_fixed_with_langchain",
        _fake_fixed_split,
    )

    service = SmartChunkingService()
    service.MAX_EMBEDDING_TEXTS = 2
    service.MAX_EMBEDDING_TOKENS = 1000

    text = "第一句。第二句。第三句。第四句。第五句。"
    result = await service.chunk_document(
        text,
        ChunkConfig(strategy=ChunkingStrategy.SEMANTIC),
        file_type="txt",
    )

    assert len(result.chunks) == 1
    assert result.chunks[0].metadata.extra["engine_mode"] == "fixed"
