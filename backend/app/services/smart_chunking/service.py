"""
智能分块 - 主服务（策略路由 + 编排）

职责：
- 根据配置选择分块策略
- 管理 embedding 缓存
- 标准化输出格式
- 提供预设配置和便捷函数

V3 变更:
  - chunk_document() 先调用 config.resolve_char_limits(text) 获取语言自适应的字符限制
  - 将 ResolvedCharLimits 注入 SemanticChunker
  - _fixed_chunking / _split_large_chunk 也使用 ResolvedCharLimits
  - stats 输出新增 token 相关指标
  - 预设配置新增 Token 字段
"""
import re
import hashlib
from dataclasses import replace
from typing import List, Dict, Optional, Any

from loguru import logger

from .metadata_builder import build_chunk_metadata, build_extra_metadata
from .external_engines import (
    ExternalChunk,
    ThirdPartyEngineUnavailable,
    split_fixed_with_langchain,
    split_hierarchical_with_llamaindex,
    split_markdown_sections_with_langchain,
    split_semantic_with_llamaindex,
)
from .types import (
    ChunkConfig, ChunkingStrategy, ChunkLevel, ChunkMetadata,
    SmartChunk, ChunkResult, EmbeddingLimitExceeded, ResolvedCharLimits,
    generate_chunk_id,
)
from .academic_detector import AcademicStructureDetector
from .semantic_chunker import SemanticChunker
from .hierarchical_chunker import HierarchicalChunker, enforce_limit
from .text_preprocessor import preprocess_text, split_to_sentences
from .token_utils import estimate_tokens as _estimate_tokens

from app.services.embedding_service import embedding_service


class SmartChunkingService:
    """
    智能分块服务 - 统一入口。

    使用示例::

        service = SmartChunkingService()
        result = await service.chunk_document(text)       # 异步（推荐）
        result = service.chunk(text, config)              # 同步（仅脚本）
    """

    MAX_EMBEDDING_TEXTS = 8192
    MAX_EMBEDDING_TOKENS = 65536

    def __init__(self, embedding_svc: Any = None):
        self.semantic_chunker: Optional[SemanticChunker] = None
        self.hierarchical_chunker: Optional[HierarchicalChunker] = None
        self._config: Optional[ChunkConfig] = None
        self._resolved: Optional[ResolvedCharLimits] = None
        self._file_type: str = "txt"
        self._source_format: str = "txt"
        self._embedding_cache: Dict[str, List[float]] = {}
        self._embedding_call_count: int = 0
        self._embedding_text_count: int = 0
        self._embedding_token_count: int = 0
        self._embedding_service = embedding_svc or embedding_service

    # ---------- 同步包装器 ----------

    def chunk(self, text: str, config: Optional[ChunkConfig] = None) -> ChunkResult:
        """同步接口 - 仅限脚本/CLI 环境。"""
        import asyncio
        try:
            asyncio.get_running_loop()
            raise RuntimeError(
                "检测到正在运行的事件循环。请使用 `await service.chunk_document()` 替代。"
            )
        except RuntimeError as e:
            if "no running event loop" not in str(e).lower() and "no current event loop" not in str(e).lower():
                raise
        return asyncio.run(self.chunk_document(text, config))

    # ---------- 文档分析 ----------

    def analyze_document(self, text: str) -> Dict[str, Any]:
        """分析文档结构，返回特征和策略推荐。"""
        is_academic = self._detect_academic_document(text)

        temp_chunker = HierarchicalChunker(ChunkConfig())
        section_boundaries = temp_chunker._detect_section_boundaries(text)

        detected_sections = [
            {"title": t, "type": st, "start": s, "end": e, "length": e - s}
            for t, s, e, st in section_boundaries
        ]

        if is_academic:
            rec_strategy, rec_reason = "academic", "检测到学术文档结构，推荐使用学术论文专用分块"
        elif len(detected_sections) >= 3:
            rec_strategy, rec_reason = "hierarchical", "检测到多个章节，推荐使用层级分块"
        elif len(text) > 10000:
            rec_strategy, rec_reason = "semantic", "长文档，推荐使用语义分块以获得更好的边界"
        else:
            rec_strategy, rec_reason = "hybrid", "通用文档，推荐使用混合策略"

        sentences = split_to_sentences(text, ChunkConfig())
        total_tokens = _estimate_tokens(text)

        return {
            "is_academic": is_academic,
            "detected_sections": detected_sections,
            "has_citations": AcademicStructureDetector.has_citations(text),
            "recommended_strategy": rec_strategy,
            "recommended_reason": rec_reason,
            "document_stats": {
                "total_chars": len(text),
                "total_tokens": total_tokens,
                "total_sentences": len(sentences),
                "total_paragraphs": text.count('\n\n') + 1,
                "avg_sentence_length": len(text) // max(len(sentences), 1),
                "section_count": len(detected_sections),
            },
            "estimated_chunks": max(1, total_tokens // 128),
            "language": "zh" if any('\u4e00' <= c <= '\u9fff' for c in text[:1000]) else "en",
        }

    def get_preset_configs(self) -> Dict[str, ChunkConfig]:
        """获取所有可用的预设配置。"""
        return {name: get_preset_config(name) for name in
                ["default", "fast", "precise", "academic", "deep"]}

    # ---------- 主入口 ----------

    async def chunk_document(
        self,
        text: str,
        config: Optional[ChunkConfig] = None,
        file_type: str = "txt"
    ) -> ChunkResult:
        """对文档进行智能分块。"""
        if not text:
            return ChunkResult(strategy="none")

        config = config or ChunkConfig()
        self._config = config
        self._file_type = self._normalize_file_type(file_type)

        # 重置请求级缓存
        self._embedding_cache = {}
        self._embedding_call_count = 0
        self._embedding_text_count = 0
        self._embedding_token_count = 0

        # V3: 解析 Token → 字符限制
        self._resolved = config.resolve_char_limits(text)
        if self._resolved.is_token_based:
            logger.info(
                f"Token 计量模式: CJK={self._resolved.cjk_ratio:.1%}, "
                f"chars_per_token={self._resolved.chars_per_token:.1f}, "
                f"base_chunk={self._resolved.base_chunk_size}chars"
            )

        # 初始化分块器 — 注入 ResolvedCharLimits
        self.semantic_chunker = SemanticChunker(
            config,
            embed_fn=self._cached_embed_texts,
            resolved_limits=self._resolved,
        )
        self.hierarchical_chunker = HierarchicalChunker(config)

        # 预处理
        preprocess_file_type = self._resolve_preprocess_file_type(text=text, file_type=self._file_type)
        self._source_format = self._resolve_source_format(
            text=text,
            file_type=self._file_type,
            preprocess_file_type=preprocess_file_type,
        )
        text = preprocess_text(text, file_type=preprocess_file_type)

        # 策略路由
        try:
            if config.strategy == ChunkingStrategy.FIXED:
                result = await self._fixed_chunking(text, config)
            elif config.strategy == ChunkingStrategy.SEMANTIC:
                result = await self._semantic_chunking(text, config)
            elif config.strategy == ChunkingStrategy.HIERARCHICAL:
                result = await self._hierarchical_chunking(text, config)
            elif config.strategy == ChunkingStrategy.ACADEMIC:
                result = await self._academic_chunking(text, config)
            else:
                result = await self._hybrid_chunking(text, config)
        except EmbeddingLimitExceeded as exc:
            logger.warning(f"Embedding 预算超限，降级到固定分块: {exc}")
            result = await self._fixed_chunking(text, config)

        # 标准化 + 构建结果
        result = self._normalize_result(result, text)
        chunks = result.get("chunks", [])

        # V3: 为每个 chunk 填充 token_count
        for chunk in chunks:
            if chunk.metadata.token_count == 0:
                chunk.metadata.token_count = _estimate_tokens(chunk.content)

        return ChunkResult(
            strategy=config.strategy.value,
            chunks=chunks,
            hierarchy=result.get("hierarchy"),
            metadata=result.get("metadata", {}),
            stats=self._calculate_stats(chunks, text)
        )

    # ---------- Embedding 缓存 ----------

    async def _cached_embed_texts(self, texts: List[str]) -> List[List[float]]:
        """带缓存的 embedding 调用。"""
        results = [None] * len(texts)
        texts_to_embed = []
        indices_to_embed = []

        for i, text in enumerate(texts):
            cache_key = hashlib.md5(text.encode()).hexdigest()
            if cache_key in self._embedding_cache:
                results[i] = self._embedding_cache[cache_key]
            else:
                texts_to_embed.append(text)
                indices_to_embed.append(i)

        if texts_to_embed:
            request_text_count = len(texts_to_embed)
            request_token_count = sum(max(1, _estimate_tokens(text)) for text in texts_to_embed)
            next_text_count = self._embedding_text_count + request_text_count
            next_token_count = self._embedding_token_count + request_token_count

            if next_text_count > int(self.MAX_EMBEDDING_TEXTS):
                raise EmbeddingLimitExceeded(
                    f"text budget exceeded: next={next_text_count}, "
                    f"limit={int(self.MAX_EMBEDDING_TEXTS)}, calls={self._embedding_call_count}"
                )
            if next_token_count > int(self.MAX_EMBEDDING_TOKENS):
                raise EmbeddingLimitExceeded(
                    f"token budget exceeded: next={next_token_count}, "
                    f"limit={int(self.MAX_EMBEDDING_TOKENS)}, texts={self._embedding_text_count}"
                )

            self._embedding_call_count += 1
            self._embedding_text_count = next_text_count
            self._embedding_token_count = next_token_count
            embeddings = await self._embedding_service.embed_texts(texts_to_embed)

            for j, idx in enumerate(indices_to_embed):
                cache_key = hashlib.md5(texts_to_embed[j].encode()).hexdigest()
                self._embedding_cache[cache_key] = embeddings[j]
                results[idx] = embeddings[j]

        return results

    # ---------- 结果标准化 ----------

    def _normalize_result(self, result: Dict[str, Any], text: str) -> Dict[str, Any]:
        """统一结果数据结构。"""
        # 确保 chunks 都是 SmartChunk
        normalized_chunks = []
        for chunk in result.get("chunks", []):
            if isinstance(chunk, SmartChunk):
                normalized_chunks.append(chunk)
            elif isinstance(chunk, dict):
                meta = chunk.get("metadata", {})
                try:
                    level = ChunkLevel(meta.get("level", "paragraph"))
                except ValueError:
                    level = ChunkLevel.PARAGRAPH

                normalized_chunks.append(SmartChunk(
                    id=chunk.get("id", ""),
                    content=chunk.get("content", ""),
                    start_char=chunk.get("start_char", 0),
                    end_char=chunk.get("end_char", 0),
                    metadata=ChunkMetadata(
                        level=level,
                        section_type=meta.get("section_type"),
                        section_title=meta.get("section_title"),
                        parent_id=meta.get("parent_id"),
                        child_ids=meta.get("child_ids", []),
                        semantic_score=meta.get("semantic_score", 0.0),
                        has_citations=meta.get("has_citations", False),
                        position_ratio=meta.get("position_ratio", 0.0),
                        keywords=meta.get("keywords", []),
                        token_count=meta.get("token_count", 0),
                        extra=meta.get("extra", {}),
                    )
                ))
        result["chunks"] = normalized_chunks

        # hierarchy 格式统一
        hierarchy = result.get("hierarchy")
        if hierarchy is not None:
            normalized_hierarchy = {}
            for key, chunks in hierarchy.items():
                str_key = key.value if isinstance(key, ChunkLevel) else str(key)
                normalized_hierarchy[str_key] = [
                    _chunk_to_dict(c) if isinstance(c, SmartChunk) else c
                    for c in chunks
                ]
            result["hierarchy"] = normalized_hierarchy

        # 补充 position_ratio
        text_len = max(len(text), 1)
        for chunk in result["chunks"]:
            if chunk.metadata.position_ratio == 0.0 and chunk.start_char > 0:
                chunk.metadata.position_ratio = round(chunk.start_char / text_len, 4)

        return result

    # ---------- 策略实现 ----------

    async def _fixed_chunking(self, text: str, config: ChunkConfig) -> Dict[str, Any]:
        """固定大小分块 - 优先使用成熟 splitter，失败时回退 legacy 实现。"""
        try:
            external_chunks = split_fixed_with_langchain(
                text=text,
                limits=self._resolved or config.resolve_char_limits(text),
            )
            if external_chunks:
                chunks = self._materialize_external_chunks(
                    external_chunks=external_chunks,
                    text=text,
                    level=ChunkLevel.PARAGRAPH,
                    engine_mode=ChunkingStrategy.FIXED.value,
                )
                return {
                    "chunks": chunks,
                    "hierarchy": None,
                    "metadata": {"engine": "langchain", "engine_mode": ChunkingStrategy.FIXED.value},
                }
        except ThirdPartyEngineUnavailable as exc:
            logger.info(f"固定分块第三方引擎不可用，回退 legacy 实现: {exc}")
        except Exception as exc:
            logger.warning(f"固定分块第三方引擎失败，回退 legacy 实现: {exc}")

        return await self._legacy_fixed_chunking(text, config)

    async def _semantic_chunking(self, text: str, config: ChunkConfig) -> Dict[str, Any]:
        """语义分块 - 优先使用 LlamaIndex 语义 splitter，失败时回退 legacy 实现。"""
        lim = self._resolved or config.resolve_char_limits(text)
        sentences, budget_reason = self._prepare_semantic_sentences(
            text=text,
            config=config,
            stage_label="语义分块",
        )
        if budget_reason:
            logger.info(f"{budget_reason}，直接降级到固定分块")
            return await self._fixed_chunking(text, config)

        try:
            external_chunks = await split_semantic_with_llamaindex(
                text=text,
                breakpoint_percentile=config.breakpoint_percentile,
                embed_texts_fn=self._cached_embed_texts,
                sentence_splitter=self._build_runtime_sentence_splitter(config),
            )
            if external_chunks:
                external_chunks = self._enforce_external_semantic_limits(
                    external_chunks=external_chunks,
                    text=text,
                    config=config,
                )
            if len(external_chunks) <= 1 and len(text) > lim.base_chunk_size * 2:
                logger.info("第三方语义分块结果不理想（仅1块），降级到固定分块")
                return await self._fixed_chunking(text, config)

            if external_chunks:
                chunks = self._materialize_external_chunks(
                    external_chunks=external_chunks,
                    text=text,
                    level=ChunkLevel.PARAGRAPH,
                    engine_mode=ChunkingStrategy.SEMANTIC.value,
                )
                return {
                    "chunks": chunks,
                    "hierarchy": None,
                    "metadata": {"engine": "llamaindex", "engine_mode": ChunkingStrategy.SEMANTIC.value},
                }
        except EmbeddingLimitExceeded:
            raise
        except ThirdPartyEngineUnavailable as exc:
            logger.info(f"语义分块第三方引擎不可用，回退 legacy 实现: {exc}")
        except Exception as exc:
            logger.warning(f"语义分块第三方引擎失败，回退 legacy 实现: {exc}")

        return await self._legacy_semantic_chunking(text, config, sentences=sentences)

    async def _hierarchical_chunking(self, text: str, config: ChunkConfig) -> Dict[str, Any]:
        """层级分块 - 优先使用第三方层级解析，失败时回退 legacy 实现。"""
        try:
            hierarchy_chunks = split_hierarchical_with_llamaindex(
                text=text,
                config=config,
                limits=self._resolved or config.resolve_char_limits(text),
            )
            paragraph_chunks = self._materialize_external_chunks(
                external_chunks=hierarchy_chunks.get("paragraph", []),
                text=text,
                level=ChunkLevel.PARAGRAPH,
                engine_mode=ChunkingStrategy.HIERARCHICAL.value,
            )
            section_chunks = self._materialize_external_chunks(
                external_chunks=hierarchy_chunks.get("section", []),
                text=text,
                level=ChunkLevel.SECTION,
                engine_mode=ChunkingStrategy.HIERARCHICAL.value,
            )
            document_chunks = self._materialize_external_chunks(
                external_chunks=hierarchy_chunks.get("document", []),
                text=text,
                level=ChunkLevel.DOCUMENT,
                engine_mode=ChunkingStrategy.HIERARCHICAL.value,
            )
            if paragraph_chunks:
                hierarchy = {
                    ChunkLevel.PARAGRAPH.value: [_chunk_to_dict(c) for c in paragraph_chunks],
                    ChunkLevel.SECTION.value: [_chunk_to_dict(c) for c in section_chunks],
                    ChunkLevel.DOCUMENT.value: [_chunk_to_dict(c) for c in document_chunks],
                }
                return {
                    "chunks": paragraph_chunks,
                    "hierarchy": hierarchy,
                    "metadata": {"engine": "llamaindex", "engine_mode": ChunkingStrategy.HIERARCHICAL.value},
                }
        except ThirdPartyEngineUnavailable as exc:
            logger.info(f"层级分块第三方引擎不可用，回退 legacy 实现: {exc}")
        except Exception as exc:
            logger.warning(f"层级分块第三方引擎失败，回退 legacy 实现: {exc}")

        return await self._legacy_hierarchical_chunking(text, config)

    async def _academic_chunking(self, text: str, config: ChunkConfig) -> Dict[str, Any]:
        """学术模式 - 保留产品编排语义，组合 markdown 章节切分与语义细分。"""
        try:
            sections = split_markdown_sections_with_langchain(text=text)
        except ThirdPartyEngineUnavailable as exc:
            logger.info(f"学术模式章节切分组件不可用，回退 legacy 实现: {exc}")
            sections = []
        except Exception as exc:
            logger.warning(f"学术模式章节切分失败，回退 legacy 实现: {exc}")
            sections = []

        if not sections:
            return await self._legacy_academic_chunking(text, config)

        chunks: list[SmartChunk] = []
        section_nodes: list[SmartChunk] = []
        hierarchy: dict[str, list[dict[str, Any]]] = {
            ChunkLevel.SECTION.value: [],
            ChunkLevel.PARAGRAPH.value: [],
        }
        detected_sections: list[str] = []

        for section in sections:
            section_title = self._resolve_external_section_title(section)
            section_type = (
                AcademicStructureDetector.detect_section_type(section_title or "")
                or AcademicStructureDetector.detect_section_type(section.content)
            )

            section_chunk = self._external_chunk_to_smart_chunk(
                external_chunk=section,
                text=text,
                level=ChunkLevel.SECTION,
                engine_mode=ChunkingStrategy.ACADEMIC.value,
                section_title=section_title,
                section_type=section_type,
            )
            section_nodes.append(section_chunk)
            hierarchy[ChunkLevel.SECTION.value].append(_chunk_to_dict(section_chunk))
            if section_type:
                detected_sections.append(section_type)

            paragraph_external_chunks = await self._split_academic_section(
                section=section,
                text=text,
                config=config,
            )
            if not paragraph_external_chunks:
                paragraph_external_chunks = [
                    ExternalChunk(
                        chunk_id=generate_chunk_id(section.content, section.start_char),
                        content=section.content,
                        start_char=section.start_char,
                        end_char=section.end_char,
                        header_path=list(section.header_path or []),
                        parent_id=section_chunk.id,
                        extra={"engine": "legacy", "splitter": "academic-section-fallback"},
                    )
                ]

            materialized = self._materialize_external_chunks(
                external_chunks=paragraph_external_chunks,
                text=text,
                level=ChunkLevel.PARAGRAPH,
                engine_mode=ChunkingStrategy.ACADEMIC.value,
                section_title=section_title,
                section_type=section_type,
                parent_id=section_chunk.id,
            )
            chunks.extend(materialized)
            hierarchy[ChunkLevel.PARAGRAPH.value].extend(_chunk_to_dict(item) for item in materialized)

        if not chunks:
            return await self._semantic_chunking(text, config)

        for section_chunk in section_nodes:
            section_chunk.metadata.child_ids = [
                chunk.id for chunk in chunks if chunk.metadata.parent_id == section_chunk.id
            ]
        hierarchy[ChunkLevel.SECTION.value] = [_chunk_to_dict(c) for c in section_nodes]

        return {
            "chunks": chunks,
            "hierarchy": hierarchy,
            "metadata": {
                "is_academic": True,
                "detected_sections": detected_sections,
                "engine": "composed",
                "engine_mode": ChunkingStrategy.ACADEMIC.value,
            },
        }

    async def _hybrid_chunking(self, text: str, config: ChunkConfig) -> Dict[str, Any]:
        """混合策略分块 - 保留系统路由能力，组合第三方与 legacy 引擎。"""
        is_academic = self._detect_academic_document(text)

        if is_academic and config.detect_academic_structure:
            result = await self._academic_chunking(text, config)
        else:
            result = await self._semantic_chunking(text, config)

        if config.enable_hierarchical and not result.get("hierarchy"):
            result["hierarchy"] = self._build_hierarchy_from_existing_chunks(text, result.get("chunks", []))

        result_metadata = dict(result.get("metadata", {}) or {})
        result_metadata.setdefault("engine_mode", ChunkingStrategy.HYBRID.value)
        result_metadata["is_academic"] = is_academic
        result["metadata"] = result_metadata
        return result

    async def _legacy_fixed_chunking(self, text: str, config: ChunkConfig) -> Dict[str, Any]:
        """固定大小分块 — 使用 Token 感知的尺寸。"""
        from app.services.document_service import TextSplitter
        lim = self._resolved or config.resolve_char_limits(text)
        splitter = TextSplitter(
            chunk_size=lim.base_chunk_size,
            chunk_overlap=lim.chunk_overlap,
        )
        raw_chunks = splitter.split_text(text)

        chunks = [
            SmartChunk(
                id=generate_chunk_id(content, start), content=content,
                start_char=start, end_char=end,
                metadata=ChunkMetadata(level=ChunkLevel.PARAGRAPH)
            )
            for content, start, end in raw_chunks
        ]
        return {"chunks": chunks, "hierarchy": None, "metadata": {}}

    async def _legacy_semantic_chunking(
        self,
        text: str,
        config: ChunkConfig,
        sentences: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """语义分块。"""
        if sentences is None:
            sentences, budget_reason = self._prepare_semantic_sentences(
                text=text,
                config=config,
                stage_label="legacy 语义分块",
            )
            if budget_reason:
                logger.info(f"{budget_reason}，直接降级到固定分块")
                return await self._fixed_chunking(text, config)

        if sentences is None:
            sentences = []

        try:
            raw_chunks = await self.semantic_chunker.chunk_by_semantics(text, sentences)
        except EmbeddingLimitExceeded:
            raise
        except Exception as e:
            logger.warning(f"语义分块失败，降级到固定分块: {e}")
            return await self._fixed_chunking(text, config)

        lim = self._resolved or config.resolve_char_limits(text)
        if len(raw_chunks) <= 1 and len(text) > lim.base_chunk_size * 2:
            logger.info("语义分块结果不理想（仅1块），降级到固定分块")
            return await self._fixed_chunking(text, config)

        chunks = [
            SmartChunk(
                id=generate_chunk_id(content, start), content=content,
                start_char=start, end_char=end,
                metadata=ChunkMetadata(
                    level=ChunkLevel.PARAGRAPH,
                    section_type=AcademicStructureDetector.detect_section_type(content),
                    has_citations=AcademicStructureDetector.has_citations(content)
                )
            )
            for content, start, end in raw_chunks
        ]
        return {"chunks": chunks, "hierarchy": None, "metadata": {}}

    async def _legacy_hierarchical_chunking(self, text: str, config: ChunkConfig) -> Dict[str, Any]:
        """层级分块。"""
        try:
            sentences, budget_reason = self._prepare_semantic_sentences(
                text=text,
                config=config,
                stage_label="层级分块语义阶段",
            )
            if budget_reason:
                raise EmbeddingLimitExceeded(budget_reason)
            raw_chunks = await self.semantic_chunker.chunk_by_semantics(text, sentences)
        except (EmbeddingLimitExceeded, Exception) as e:
            logger.warning(f"层级分块中语义分块受限或失败: {e}")
            from app.services.document_service import TextSplitter
            lim = self._resolved or config.resolve_char_limits(text)
            splitter = TextSplitter(
                chunk_size=lim.base_chunk_size,
                chunk_overlap=lim.chunk_overlap,
            )
            raw_chunks = splitter.split_text(text)

        hierarchy = self.hierarchical_chunker.create_hierarchy(text, raw_chunks)
        main_chunks = hierarchy.get(ChunkLevel.PARAGRAPH, [])

        return {
            "chunks": main_chunks,
            "hierarchy": {
                level.value: [_chunk_to_dict(c) for c in chunks]
                for level, chunks in hierarchy.items()
            },
            "metadata": {}
        }

    async def _legacy_academic_chunking(self, text: str, config: ChunkConfig) -> Dict[str, Any]:
        """学术论文专用分块。"""
        section_boundaries = self.hierarchical_chunker._detect_section_boundaries(text)

        chunks = []
        hierarchy = {ChunkLevel.SECTION.value: [], ChunkLevel.PARAGRAPH.value: []}

        for title, start, end, section_type in section_boundaries:
            section_text = text[start:end]
            section_parts = enforce_limit(section_text, 2000)

            for i, (part_content, part_start, part_end) in enumerate(section_parts):
                abs_start = start + part_start
                abs_end = start + part_end
                part_id = generate_chunk_id(part_content, abs_start)

                section_chunk = SmartChunk(
                    id=part_id, content=part_content,
                    start_char=abs_start, end_char=abs_end,
                    metadata=ChunkMetadata(
                        level=ChunkLevel.SECTION,
                        section_type=section_type,
                        section_title=(f"{title} (Part {i+1}/{len(section_parts)})"
                                      if len(section_parts) > 1 else title),
                        has_citations=AcademicStructureDetector.has_citations(part_content)
                    )
                )
                hierarchy[ChunkLevel.SECTION.value].append(_chunk_to_dict(section_chunk))

                try:
                    sentences, budget_reason = self._prepare_semantic_sentences(
                        text=part_content,
                        config=config,
                        stage_label="学术章节语义分块",
                    )
                    if budget_reason:
                        raise EmbeddingLimitExceeded(budget_reason)
                    section_chunks = await self.semantic_chunker.chunk_by_semantics(part_content, sentences)
                except (EmbeddingLimitExceeded, Exception) as e:
                    logger.warning(f"章节内语义分块受限: {e}")
                    from app.services.document_service import TextSplitter
                    lim = self._resolved or config.resolve_char_limits(text)
                    splitter = TextSplitter(
                        chunk_size=lim.base_chunk_size,
                        chunk_overlap=lim.chunk_overlap,
                    )
                    section_chunks = splitter.split_text(part_content)

                for content, sub_start, sub_end in section_chunks:
                    chunk_abs_start = abs_start + sub_start
                    chunk_abs_end = abs_start + sub_end
                    chunk = SmartChunk(
                        id=generate_chunk_id(content, chunk_abs_start), content=content,
                        start_char=chunk_abs_start, end_char=chunk_abs_end,
                        metadata=ChunkMetadata(
                            level=ChunkLevel.PARAGRAPH,
                            section_type=section_type,
                            section_title=section_chunk.metadata.section_title,
                            parent_id=part_id,
                            has_citations=AcademicStructureDetector.has_citations(content)
                        )
                    )
                    chunks.append(chunk)
                    hierarchy[ChunkLevel.PARAGRAPH.value].append(_chunk_to_dict(chunk))

        if not chunks:
            return await self._semantic_chunking(text, config)

        return {
            "chunks": chunks, "hierarchy": hierarchy,
            "metadata": {"is_academic": True,
                         "detected_sections": [b[3] for b in section_boundaries if b[3]]}
        }

    # ---------- 内部工具 ----------

    def _materialize_external_chunks(
        self,
        *,
        external_chunks: List[ExternalChunk],
        text: str,
        level: ChunkLevel,
        engine_mode: str,
        section_title: Optional[str] = None,
        section_type: Optional[str] = None,
        parent_id: Optional[str] = None,
    ) -> List[SmartChunk]:
        return [
            self._external_chunk_to_smart_chunk(
                external_chunk=chunk,
                text=text,
                level=level,
                engine_mode=engine_mode,
                section_title=section_title,
                section_type=section_type,
                parent_id=parent_id,
            )
            for chunk in list(external_chunks or [])
            if str(chunk.content or "").strip()
        ]

    def _external_chunk_to_smart_chunk(
        self,
        *,
        external_chunk: ExternalChunk,
        text: str,
        level: ChunkLevel,
        engine_mode: str,
        section_title: Optional[str] = None,
        section_type: Optional[str] = None,
        parent_id: Optional[str] = None,
    ) -> SmartChunk:
        text_len = max(len(text), 1)
        header_path = list(external_chunk.header_path or [])
        resolved_section_title = (
            section_title
            or self._resolve_external_section_title(external_chunk)
            or (header_path[-1] if header_path else None)
        )
        extra = dict(external_chunk.extra or {})
        engine = str(extra.pop("engine", "legacy") or "legacy")
        splitter = str(extra.pop("splitter", "") or "")
        metadata = build_chunk_metadata(
            level=level,
            content=external_chunk.content,
            position_ratio=round(external_chunk.start_char / text_len, 4),
            section_title=resolved_section_title,
            section_type=section_type,
            parent_id=parent_id if parent_id is not None else external_chunk.parent_id,
            child_ids=list(external_chunk.child_ids or []),
            extra=build_extra_metadata(
                engine=engine,
                engine_mode=engine_mode,
                splitter=splitter,
                source_format=self._source_format,
                start_char=external_chunk.start_char,
                end_char=external_chunk.end_char,
                header_path=header_path,
                prev_id=external_chunk.prev_id,
                next_id=external_chunk.next_id,
                content=external_chunk.content,
                extra=extra,
            ),
        )
        metadata.token_count = _estimate_tokens(external_chunk.content)
        return SmartChunk(
            id=external_chunk.chunk_id,
            content=external_chunk.content,
            start_char=external_chunk.start_char,
            end_char=external_chunk.end_char,
            metadata=metadata,
        )

    async def _split_academic_section(
        self,
        *,
        section: ExternalChunk,
        text: str,
        config: ChunkConfig,
    ) -> List[ExternalChunk]:
        _, budget_reason = self._prepare_semantic_sentences(
            text=section.content,
            config=config,
            stage_label="学术模式章节内语义切分",
        )
        if budget_reason:
            logger.info(f"{budget_reason}，直接降级到固定切分")
            return self._split_academic_section_fixed(section=section, config=config)

        try:
            chunks = await split_semantic_with_llamaindex(
                text=section.content,
                breakpoint_percentile=config.breakpoint_percentile,
                embed_texts_fn=self._cached_embed_texts,
                sentence_splitter=self._build_runtime_sentence_splitter(config),
                offset=section.start_char,
            )
            chunks = self._merge_external_chunk_headers(chunks=chunks, header_path=section.header_path)
            return self._enforce_external_semantic_limits(
                external_chunks=chunks,
                text=text,
                config=config,
            )
        except EmbeddingLimitExceeded:
            logger.info("学术模式章节内语义切分运行时超预算，降级到固定切分")
        except ThirdPartyEngineUnavailable as exc:
            logger.info(f"学术模式章节内语义切分组件不可用，回退固定切分: {exc}")
        except Exception as exc:
            logger.warning(f"学术模式章节内语义切分失败，回退固定切分: {exc}")

        return self._split_academic_section_fixed(section=section, config=config)

    def _split_academic_section_fixed(
        self,
        *,
        section: ExternalChunk,
        config: ChunkConfig,
    ) -> List[ExternalChunk]:
        try:
            chunks = split_fixed_with_langchain(
                text=section.content,
                limits=self._resolved or config.resolve_char_limits(section.content),
            )
            return self._offset_external_chunks(
                chunks=self._merge_external_chunk_headers(chunks=chunks, header_path=section.header_path),
                offset=section.start_char,
                parent_id=section.parent_id,
            )
        except ThirdPartyEngineUnavailable:
            return []
        except Exception as exc:
            logger.warning(f"学术模式章节内固定切分失败: {exc}")
            return []

    def _prepare_semantic_sentences(
        self,
        *,
        text: str,
        config: ChunkConfig,
        stage_label: str,
    ) -> tuple[List[str], Optional[str]]:
        sentences = split_to_sentences(text, config)
        if len(sentences) < 2:
            return sentences, None

        over_budget_reason = self._check_embedding_budget_for_texts(
            sentences,
            stage_label=stage_label,
        )
        return sentences, over_budget_reason

    def _build_runtime_sentence_splitter(
        self,
        config: ChunkConfig,
    ):
        limits = self._resolved
        if limits is None:
            return None

        runtime_config = replace(
            config,
            use_token_based=False,
            base_chunk_size=int(limits.base_chunk_size),
            chunk_overlap=int(limits.chunk_overlap),
            min_semantic_chunk=int(limits.min_semantic_chunk),
            max_semantic_chunk=int(limits.max_semantic_chunk),
        )

        def _sentence_splitter(payload: str) -> List[str]:
            return split_to_sentences(
                payload,
                runtime_config,
                max_semantic_chars=int(limits.max_semantic_chunk),
            )

        return _sentence_splitter

    def _enforce_external_semantic_limits(
        self,
        *,
        external_chunks: List[ExternalChunk],
        text: str,
        config: ChunkConfig,
    ) -> List[ExternalChunk]:
        if not external_chunks:
            return []

        limits = self._resolved or config.resolve_char_limits(text)
        adjusted = self._split_oversized_external_chunks(
            external_chunks=external_chunks,
            text=text,
            config=config,
            limits=limits,
        )
        adjusted = self._merge_small_external_chunks(
            external_chunks=adjusted,
            text=text,
            config=config,
            limits=limits,
        )
        return self._relink_external_chunks(adjusted)

    def _split_oversized_external_chunks(
        self,
        *,
        external_chunks: List[ExternalChunk],
        text: str,
        config: ChunkConfig,
        limits: ResolvedCharLimits,
    ) -> List[ExternalChunk]:
        normalized: List[ExternalChunk] = []
        for chunk in list(external_chunks or []):
            if self._chunk_exceeds_max_limit(chunk.content, config, limits):
                normalized.extend(
                    self._split_external_chunk_by_limit(
                        external_chunk=chunk,
                        text=text,
                        config=config,
                        limits=limits,
                    )
                )
            else:
                normalized.append(chunk)
        return normalized

    def _split_external_chunk_by_limit(
        self,
        *,
        external_chunk: ExternalChunk,
        text: str,
        config: ChunkConfig,
        limits: ResolvedCharLimits,
        target_chars: Optional[int] = None,
    ) -> List[ExternalChunk]:
        preferred_chars = int(
            min(
                max(32, limits.max_semantic_chunk),
                max(32, limits.base_chunk_size, limits.min_semantic_chunk),
            )
        )
        split_chars = max(32, int(target_chars or preferred_chars))
        start_char = int(external_chunk.start_char)
        end_char = int(external_chunk.end_char)

        if end_char <= start_char:
            return []

        pieces: List[ExternalChunk] = []
        cursor = start_char
        while cursor < end_char:
            split_end = self._choose_split_end(
                text=text,
                start_char=cursor,
                end_char=end_char,
                target_chars=split_chars,
            )
            piece_start, piece_end = self._trim_range(text, cursor, split_end)
            if piece_end <= piece_start:
                piece_start = cursor
                piece_end = min(end_char, max(cursor + 1, split_end))
            piece = self._slice_external_chunk(
                external_chunk=external_chunk,
                text=text,
                start_char=piece_start,
                end_char=piece_end,
                postprocess_step="split_large_semantic",
            )
            if (
                piece is not None
                and self._chunk_exceeds_max_limit(piece.content, config, limits)
                and (piece.end_char - piece.start_char) > 64
                and split_chars > 32
            ):
                pieces.extend(
                    self._split_external_chunk_by_limit(
                        external_chunk=piece,
                        text=text,
                        config=config,
                        limits=limits,
                        target_chars=max(32, split_chars // 2),
                    )
                )
            elif piece is not None:
                pieces.append(piece)

            if split_end >= end_char:
                break

            next_cursor = max(piece_end - int(limits.chunk_overlap), cursor + 1)
            if next_cursor <= cursor:
                next_cursor = split_end
            cursor = next_cursor

        return pieces or [external_chunk]

    def _merge_small_external_chunks(
        self,
        *,
        external_chunks: List[ExternalChunk],
        text: str,
        config: ChunkConfig,
        limits: ResolvedCharLimits,
    ) -> List[ExternalChunk]:
        adjusted = list(external_chunks or [])
        if len(adjusted) < 2:
            return adjusted

        changed = True
        while changed and len(adjusted) > 1:
            changed = False
            merged: List[ExternalChunk] = []
            idx = 0
            while idx < len(adjusted):
                current = adjusted[idx]
                if not self._chunk_below_min_limit(current.content, config, limits):
                    merged.append(current)
                    idx += 1
                    continue

                if merged:
                    candidate = self._merge_external_chunks(
                        left=merged[-1],
                        right=current,
                        text=text,
                    )
                    if not self._chunk_exceeds_max_limit(candidate.content, config, limits):
                        merged[-1] = candidate
                        idx += 1
                        changed = True
                        continue

                next_chunk = adjusted[idx + 1] if idx + 1 < len(adjusted) else None
                if next_chunk is not None:
                    candidate = self._merge_external_chunks(
                        left=current,
                        right=next_chunk,
                        text=text,
                    )
                    if not self._chunk_exceeds_max_limit(candidate.content, config, limits):
                        merged.append(candidate)
                        idx += 2
                        changed = True
                        continue

                merged.append(current)
                idx += 1

            adjusted = merged

        return adjusted

    def _slice_external_chunk(
        self,
        *,
        external_chunk: ExternalChunk,
        text: str,
        start_char: int,
        end_char: int,
        postprocess_step: str,
    ) -> Optional[ExternalChunk]:
        trimmed_start, trimmed_end = self._trim_range(text, start_char, end_char)
        if trimmed_end <= trimmed_start:
            return None

        content = text[trimmed_start:trimmed_end]
        extra = dict(external_chunk.extra or {})
        postprocess_steps = list(extra.get("postprocess_steps", []))
        if postprocess_step not in postprocess_steps:
            postprocess_steps.append(postprocess_step)
        extra["postprocess_steps"] = postprocess_steps

        return ExternalChunk(
            chunk_id=generate_chunk_id(content, trimmed_start),
            content=content,
            start_char=trimmed_start,
            end_char=trimmed_end,
            header_path=list(external_chunk.header_path or []),
            prev_id=external_chunk.prev_id,
            next_id=external_chunk.next_id,
            parent_id=external_chunk.parent_id,
            child_ids=list(external_chunk.child_ids or []),
            extra=extra,
        )

    def _merge_external_chunks(
        self,
        *,
        left: ExternalChunk,
        right: ExternalChunk,
        text: str,
    ) -> ExternalChunk:
        start_char = min(int(left.start_char), int(right.start_char))
        end_char = max(int(left.end_char), int(right.end_char))
        merged_start, merged_end = self._trim_range(text, start_char, end_char)
        content = text[merged_start:merged_end]

        extra = dict(left.extra or {})
        for key, value in dict(right.extra or {}).items():
            extra.setdefault(key, value)
        postprocess_steps = list(extra.get("postprocess_steps", []))
        if "merge_small_semantic" not in postprocess_steps:
            postprocess_steps.append("merge_small_semantic")
        extra["postprocess_steps"] = postprocess_steps

        return ExternalChunk(
            chunk_id=generate_chunk_id(content, merged_start),
            content=content,
            start_char=merged_start,
            end_char=merged_end,
            header_path=self._combine_header_paths(left.header_path, right.header_path),
            parent_id=left.parent_id or right.parent_id,
            child_ids=list(left.child_ids or right.child_ids or []),
            extra=extra,
        )

    def _relink_external_chunks(self, chunks: List[ExternalChunk]) -> List[ExternalChunk]:
        relinked: List[ExternalChunk] = []
        for idx, chunk in enumerate(list(chunks or [])):
            relinked.append(
                ExternalChunk(
                    chunk_id=chunk.chunk_id,
                    content=chunk.content,
                    start_char=chunk.start_char,
                    end_char=chunk.end_char,
                    header_path=list(chunk.header_path or []),
                    prev_id=chunks[idx - 1].chunk_id if idx > 0 else None,
                    next_id=chunks[idx + 1].chunk_id if idx < len(chunks) - 1 else None,
                    parent_id=chunk.parent_id,
                    child_ids=list(chunk.child_ids or []),
                    extra=dict(chunk.extra or {}),
                )
            )
        return relinked

    @staticmethod
    def _trim_range(text: str, start_char: int, end_char: int) -> tuple[int, int]:
        start = max(0, int(start_char))
        end = min(len(text), int(end_char))
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        return start, end

    @staticmethod
    def _choose_split_end(
        *,
        text: str,
        start_char: int,
        end_char: int,
        target_chars: int,
    ) -> int:
        preferred_end = min(end_char, start_char + max(1, int(target_chars)))
        if preferred_end >= end_char:
            return end_char

        search_start = max(start_char + max(1, int(target_chars // 2)), start_char + 1)
        for separator in ["\n\n", "\n", "。", "！", "？", ". ", "! ", "? ", "；", ";", "，", ",", " "]:
            split_pos = text.rfind(separator, search_start, preferred_end)
            if split_pos != -1:
                return split_pos + len(separator)
        return preferred_end

    def _chunk_below_min_limit(
        self,
        content: str,
        config: ChunkConfig,
        limits: ResolvedCharLimits,
    ) -> bool:
        if config.use_token_based:
            return max(1, _estimate_tokens(content)) < max(1, int(config.min_semantic_tokens))
        return len(content) < max(1, int(limits.min_semantic_chunk))

    def _chunk_exceeds_max_limit(
        self,
        content: str,
        config: ChunkConfig,
        limits: ResolvedCharLimits,
    ) -> bool:
        if config.use_token_based:
            return (
                max(1, _estimate_tokens(content)) > max(1, int(config.max_semantic_tokens))
                or len(content) > max(1, int(limits.max_semantic_chunk))
            )
        return len(content) > max(1, int(limits.max_semantic_chunk))

    @staticmethod
    def _combine_header_paths(left: Optional[List[str]], right: Optional[List[str]]) -> List[str]:
        resolved: List[str] = []
        seen: set[str] = set()
        for value in list(left or []) + list(right or []):
            item = str(value or "").strip()
            if not item or item in seen:
                continue
            seen.add(item)
            resolved.append(item)
        return resolved

    def _check_embedding_budget_for_texts(
        self,
        texts: List[str],
        *,
        stage_label: str,
    ) -> Optional[str]:
        pending_text_count, pending_token_count = self._estimate_uncached_embedding_load(texts)
        remaining_text_budget = max(0, int(self.MAX_EMBEDDING_TEXTS) - int(self._embedding_text_count))
        remaining_token_budget = max(0, int(self.MAX_EMBEDDING_TOKENS) - int(self._embedding_token_count))

        if pending_text_count > remaining_text_budget:
            return (
                f"{stage_label}预估 embedding 文本预算不足"
                f"(pending_texts={pending_text_count}, remaining_text_budget={remaining_text_budget})"
            )
        if pending_token_count > remaining_token_budget:
            return (
                f"{stage_label}预估 embedding token 预算不足"
                f"(pending_tokens={pending_token_count}, remaining_token_budget={remaining_token_budget})"
            )
        return None

    def _estimate_uncached_embedding_load(self, texts: List[str]) -> tuple[int, int]:
        seen_cache_keys: set[str] = set()
        pending_text_count = 0
        pending_token_count = 0

        for raw_text in list(texts or []):
            normalized_text = str(raw_text or "").strip()
            if not normalized_text:
                continue
            cache_key = hashlib.md5(normalized_text.encode()).hexdigest()
            if cache_key in self._embedding_cache or cache_key in seen_cache_keys:
                continue
            seen_cache_keys.add(cache_key)
            pending_text_count += 1
            pending_token_count += max(1, _estimate_tokens(normalized_text))

        return pending_text_count, pending_token_count

    def _offset_external_chunks(
        self,
        *,
        chunks: List[ExternalChunk],
        offset: int,
        parent_id: Optional[str] = None,
    ) -> List[ExternalChunk]:
        return [
            ExternalChunk(
                chunk_id=chunk.chunk_id,
                content=chunk.content,
                start_char=chunk.start_char + offset,
                end_char=chunk.end_char + offset,
                header_path=list(chunk.header_path or []),
                prev_id=chunk.prev_id,
                next_id=chunk.next_id,
                parent_id=parent_id if parent_id is not None else chunk.parent_id,
                child_ids=list(chunk.child_ids or []),
                extra=dict(chunk.extra or {}),
            )
            for chunk in list(chunks or [])
        ]

    @staticmethod
    def _merge_external_chunk_headers(
        *,
        chunks: List[ExternalChunk],
        header_path: Optional[List[str]],
    ) -> List[ExternalChunk]:
        resolved_header_path = list(header_path or [])
        if not resolved_header_path:
            return list(chunks or [])
        return [
            ExternalChunk(
                chunk_id=chunk.chunk_id,
                content=chunk.content,
                start_char=chunk.start_char,
                end_char=chunk.end_char,
                header_path=list(chunk.header_path or resolved_header_path),
                prev_id=chunk.prev_id,
                next_id=chunk.next_id,
                parent_id=chunk.parent_id,
                child_ids=list(chunk.child_ids or []),
                extra=dict(chunk.extra or {}),
            )
            for chunk in list(chunks or [])
        ]

    def _build_hierarchy_from_existing_chunks(
        self,
        text: str,
        chunks: List[SmartChunk],
    ) -> Dict[str, List[Dict[str, Any]]]:
        try:
            raw_chunks = [(chunk.content, chunk.start_char, chunk.end_char) for chunk in list(chunks or [])]
            hierarchy = self.hierarchical_chunker.create_hierarchy(text, raw_chunks)
            preserved_paragraph_chunks = [
                SmartChunk(
                    id=chunk.id,
                    content=chunk.content,
                    start_char=chunk.start_char,
                    end_char=chunk.end_char,
                    metadata=ChunkMetadata(
                        level=chunk.metadata.level,
                        section_type=chunk.metadata.section_type,
                        section_title=chunk.metadata.section_title,
                        parent_id=chunk.metadata.parent_id,
                        child_ids=list(chunk.metadata.child_ids or []),
                        semantic_score=chunk.metadata.semantic_score,
                        has_citations=chunk.metadata.has_citations,
                        position_ratio=chunk.metadata.position_ratio,
                        keywords=list(chunk.metadata.keywords or []),
                        token_count=chunk.metadata.token_count,
                        extra=dict(chunk.metadata.extra or {}),
                    ),
                )
                for chunk in list(chunks or [])
            ]
            if ChunkLevel.SECTION in hierarchy:
                self.hierarchical_chunker._link_parent_child(
                    hierarchy[ChunkLevel.SECTION],
                    preserved_paragraph_chunks,
                )
            elif ChunkLevel.DOCUMENT in hierarchy:
                self.hierarchical_chunker._link_parent_child(
                    hierarchy[ChunkLevel.DOCUMENT],
                    preserved_paragraph_chunks,
                )
            hierarchy[ChunkLevel.PARAGRAPH] = preserved_paragraph_chunks
            return {
                level.value: [_chunk_to_dict(chunk) for chunk in items]
                for level, items in hierarchy.items()
            }
        except Exception as exc:
            logger.warning(f"构建层级上下文失败，返回空 hierarchy: {exc}")
            return {}

    @staticmethod
    def _resolve_external_section_title(external_chunk: ExternalChunk) -> Optional[str]:
        header_path = [str(item).strip() for item in list(external_chunk.header_path or []) if str(item).strip()]
        if header_path:
            return header_path[-1]
        extracted = AcademicStructureDetector.extract_section_title(external_chunk.content)
        return extracted.strip() if isinstance(extracted, str) and extracted.strip() else None

    @staticmethod
    def _normalize_file_type(file_type: str) -> str:
        return str(file_type or "txt").strip().lower().replace(".", "")

    def _resolve_preprocess_file_type(self, *, text: str, file_type: str) -> str:
        if self._normalize_file_type(file_type) == "pdf" and self._looks_like_markdown_document(text):
            return "md"
        return self._normalize_file_type(file_type)

    def _resolve_source_format(
        self,
        *,
        text: str,
        file_type: str,
        preprocess_file_type: str,
    ) -> str:
        normalized_file_type = self._normalize_file_type(file_type)
        if normalized_file_type == "pdf" and self._looks_like_markdown_document(text):
            return "pdf_ingest_md"
        if preprocess_file_type in {"md", "markdown"}:
            return "md"
        return normalized_file_type or "txt"

    @staticmethod
    def _looks_like_markdown_document(text: str) -> bool:
        payload = str(text or "")
        return bool(
            re.search(r"(?m)^\s{0,3}#{1,6}\s+\S", payload)
            or re.search(r"(?m)^\s*\|.+\|\s*$", payload)
            or re.search(r"(?m)^\s*(?:[-*+•]|\d+[\.\)])\s+\S", payload)
            or "```" in payload
        )

    def _detect_academic_document(self, text: str) -> bool:
        """检测是否为学术文档。"""
        academic_markers = 0
        for section_type in ['abstract', 'introduction', 'methodology', 'conclusion', 'references']:
            for pattern in AcademicStructureDetector.SECTION_PATTERNS.get(section_type, []):
                if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
                    academic_markers += 1
                    break
        if AcademicStructureDetector.has_citations(text):
            academic_markers += 1
        return academic_markers >= 2

    @staticmethod
    def _calculate_stats(chunks: List[SmartChunk], text: str) -> Dict[str, Any]:
        if not chunks:
            return {}
        sizes = [len(c.content) for c in chunks]
        token_sizes = [c.metadata.token_count for c in chunks]
        return {
            "total_chunks": len(chunks),
            "total_chars": len(text),
            "total_tokens": _estimate_tokens(text),
            "avg_chunk_size": sum(sizes) // len(sizes),
            "min_chunk_size": min(sizes),
            "max_chunk_size": max(sizes),
            "avg_chunk_tokens": sum(token_sizes) // len(token_sizes) if token_sizes else 0,
            "min_chunk_tokens": min(token_sizes) if token_sizes else 0,
            "max_chunk_tokens": max(token_sizes) if token_sizes else 0,
            "chunks_with_citations": sum(1 for c in chunks if c.metadata.has_citations),
        }

    # 向后兼容：旧代码中 service._split_to_sentences(text) 的调用
    def _split_to_sentences(self, text: str) -> List[str]:
        return split_to_sentences(text, self._config or ChunkConfig())

    def _preprocess_text(self, text: str, file_type: str = "txt") -> str:
        return preprocess_text(text, file_type=file_type)

    def _empty_result(self) -> Dict[str, Any]:
        """Backward-compatible empty result structure used by historical tests."""
        return {"chunks": [], "hierarchy": None, "metadata": {}}

    def _chunk_to_dict(self, chunk: SmartChunk) -> Dict[str, Any]:
        return _chunk_to_dict(chunk)


# ============== 模块级工具 ==============

def _chunk_to_dict(chunk: SmartChunk) -> Dict[str, Any]:
    """将 SmartChunk 转换为字典。"""
    return {
        "id": chunk.id,
        "content": chunk.content,
        "start_char": chunk.start_char,
        "end_char": chunk.end_char,
        "metadata": {
            "level": chunk.metadata.level.value,
            "section_type": chunk.metadata.section_type,
            "section_title": chunk.metadata.section_title,
            "parent_id": chunk.metadata.parent_id,
            "child_ids": chunk.metadata.child_ids,
            "semantic_score": chunk.metadata.semantic_score,
            "has_citations": chunk.metadata.has_citations,
            "position_ratio": chunk.metadata.position_ratio,
            "keywords": chunk.metadata.keywords,
            "token_count": chunk.metadata.token_count,
            "extra": chunk.metadata.extra,
        }
    }


# ============== 工厂 + 代理 + 便捷函数 ==============

def create_chunking_service(embedding_svc: Any = None) -> SmartChunkingService:
    """工厂函数：每次创建新实例，确保并发安全。"""
    return SmartChunkingService(embedding_svc=embedding_svc)


class _ServiceProxy:
    """向后兼容代理：让旧代码 ``smart_chunking_service.xxx()`` 继续工作。"""
    def __getattr__(self, name):
        return getattr(SmartChunkingService(), name)

smart_chunking_service = _ServiceProxy()


async def chunk_document_smart(text: str, strategy: str = "hybrid", **kwargs) -> Dict[str, Any]:
    """便捷函数 - 智能分块文档。"""
    config = ChunkConfig(
        strategy=ChunkingStrategy(strategy),
        **{k: v for k, v in kwargs.items() if hasattr(ChunkConfig, k)}
    )
    service = create_chunking_service()
    return await service.chunk_document(text, config)


def get_preset_config(preset: str) -> ChunkConfig:
    """
    获取预设配置。

    V3: 所有预设默认 use_token_based=True，
    同时保留字符字段以兼容旧代码路径。
    """
    presets = {
        "default": ChunkConfig(
            use_token_based=True,
            base_chunk_tokens=128,
            overlap_tokens=16,
            min_semantic_tokens=32,
            max_semantic_tokens=384,
        ),
        "fast": ChunkConfig(
            strategy=ChunkingStrategy.FIXED,
            enable_hierarchical=False,
            use_token_based=True,
            base_chunk_tokens=128,
            overlap_tokens=16,
            min_semantic_tokens=32,
            max_semantic_tokens=384,
        ),
        "precise": ChunkConfig(
            strategy=ChunkingStrategy.SEMANTIC,
            breakpoint_percentile=90.0,
            min_semantic_chunk=150,
            enable_hierarchical=False,
            use_token_based=True,
            base_chunk_tokens=128,
            overlap_tokens=16,
            min_semantic_tokens=48,
            max_semantic_tokens=384,
        ),
        "academic": ChunkConfig(
            strategy=ChunkingStrategy.ACADEMIC,
            detect_academic_structure=True,
            preserve_citations=True,
            enable_hierarchical=True,
            use_token_based=True,
            base_chunk_tokens=128,
            overlap_tokens=16,
            min_semantic_tokens=32,
            max_semantic_tokens=384,
        ),
        "deep": ChunkConfig(
            strategy=ChunkingStrategy.HIERARCHICAL,
            enable_hierarchical=True,
            hierarchy_levels=[ChunkLevel.PARAGRAPH, ChunkLevel.SECTION, ChunkLevel.DOCUMENT],
            use_token_based=True,
            base_chunk_tokens=128,
            overlap_tokens=16,
            min_semantic_tokens=32,
            max_semantic_tokens=384,
        ),
    }
    return presets.get(preset, presets["default"])
