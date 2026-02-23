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
from typing import List, Dict, Optional, Any

from loguru import logger

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

    MAX_EMBEDDING_CALLS = 20

    def __init__(self):
        self.semantic_chunker: Optional[SemanticChunker] = None
        self.hierarchical_chunker: Optional[HierarchicalChunker] = None
        self._config: Optional[ChunkConfig] = None
        self._resolved: Optional[ResolvedCharLimits] = None
        self._embedding_cache: Dict[str, List[float]] = {}
        self._embedding_call_count: int = 0

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

        # 重置请求级缓存
        self._embedding_cache = {}
        self._embedding_call_count = 0

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
        text = preprocess_text(text, file_type=file_type)

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
        except EmbeddingLimitExceeded:
            logger.warning("Embedding 调用次数超限，降级到固定分块")
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
            if self._embedding_call_count >= self.MAX_EMBEDDING_CALLS:
                raise EmbeddingLimitExceeded()

            self._embedding_call_count += 1
            embeddings = await embedding_service.embed_texts(texts_to_embed)

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
                        has_citations=meta.get("has_citations", False),
                        position_ratio=meta.get("position_ratio", 0.0),
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

    async def _semantic_chunking(self, text: str, config: ChunkConfig) -> Dict[str, Any]:
        """语义分块。"""
        sentences = split_to_sentences(text, config)

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

    async def _hierarchical_chunking(self, text: str, config: ChunkConfig) -> Dict[str, Any]:
        """层级分块。"""
        try:
            sentences = split_to_sentences(text, config)
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

    async def _academic_chunking(self, text: str, config: ChunkConfig) -> Dict[str, Any]:
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

                sentences = split_to_sentences(part_content, config)
                try:
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

    async def _hybrid_chunking(self, text: str, config: ChunkConfig) -> Dict[str, Any]:
        """混合策略分块。"""
        is_academic = self._detect_academic_document(text)

        if is_academic and config.detect_academic_structure:
            result = await self._academic_chunking(text, config)
        else:
            result = await self._semantic_chunking(text, config)

        if config.enable_hierarchical:
            raw_chunks = [(c.content, c.start_char, c.end_char) for c in result["chunks"]]
            hierarchy = self.hierarchical_chunker.create_hierarchy(text, raw_chunks)
            result["hierarchy"] = {
                level.value: [_chunk_to_dict(c) for c in chunks]
                for level, chunks in hierarchy.items()
            }

        return result

    # ---------- 内部工具 ----------

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
            "has_citations": chunk.metadata.has_citations,
            "position_ratio": chunk.metadata.position_ratio,
            "token_count": chunk.metadata.token_count,
        }
    }


# ============== 工厂 + 代理 + 便捷函数 ==============

def create_chunking_service() -> SmartChunkingService:
    """工厂函数：每次创建新实例，确保并发安全。"""
    return SmartChunkingService()


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
