"""
智能分块 - 语义分块器

V2 算法：相邻句子 embedding 余弦距离 + 百分位断点检测。
对齐业界标准（LlamaIndex SemanticSplitter / Greg Kamradt）。

V3 变更:
  - 内部使用 ResolvedCharLimits 而非直接读取 ChunkConfig 的字符字段
  - 通过 Token 计量模式，中英文混合文档获得一致的信息密度分块
"""
import numpy as np
from typing import List, Tuple, Optional, Callable

from loguru import logger

from .types import ChunkConfig, ResolvedCharLimits
from .academic_detector import AcademicStructureDetector


class SemanticChunker:
    """基于语义相似度的分块器"""

    def __init__(
        self,
        config: ChunkConfig,
        embed_fn: Optional[Callable] = None,
        resolved_limits: Optional[ResolvedCharLimits] = None,
    ):
        self.config = config
        self._resolved = resolved_limits  # 由 service 层注入
        from app.services.embedding_service import embedding_service
        self._embed_fn = embed_fn or embedding_service.embed_texts

    @property
    def limits(self) -> ResolvedCharLimits:
        """获取解析后的字符限制（延迟回退）"""
        if self._resolved is not None:
            return self._resolved
        return self.config.resolve_char_limits()

    async def detect_semantic_boundaries(
        self,
        sentences: List[str]
    ) -> List[int]:
        """
        检测语义边界 — 相邻句子余弦距离 + 百分位断点检测。

        算法：
        1. 对每个句子生成 embedding
        2. 计算每对相邻句子 embedding 的余弦距离 (1 - cosine_similarity)
        3. 取距离分布的高百分位（如 P95）作为阈值
        4. 距离超过阈值的位置即为语义边界

        返回: 边界位置索引列表（在第 i 个句子前切分）
        """
        if len(sentences) < 2:
            return []

        embeddings = await self._embed_fn(sentences)

        if not embeddings or len(embeddings) != len(sentences):
            logger.warning("获取句子嵌入失败，回退到固定分块")
            return []

        # Step 1: 计算每对相邻句子的余弦距离
        distances = []
        for i in range(len(embeddings) - 1):
            vec_a = np.array(embeddings[i])
            vec_b = np.array(embeddings[i + 1])
            norm_a = np.linalg.norm(vec_a)
            norm_b = np.linalg.norm(vec_b)
            if norm_a == 0 or norm_b == 0:
                distances.append(1.0)
            else:
                sim = float(np.dot(vec_a, vec_b) / (norm_a * norm_b))
                distances.append(1.0 - sim)

        if not distances:
            return []

        dist_array = np.array(distances)

        # Step 2: 百分位断点阈值
        threshold = float(np.percentile(dist_array, self.config.breakpoint_percentile))

        # Step 3: 超过阈值的位置即为边界
        boundaries = []
        for i, dist in enumerate(distances):
            if dist > threshold:
                boundary_pos = i + 1
                if not boundaries or (boundary_pos - boundaries[-1]) >= 2:
                    boundaries.append(boundary_pos)

        return boundaries

    async def chunk_by_semantics(
        self,
        text: str,
        sentences: List[str]
    ) -> List[Tuple[str, int, int]]:
        """
        按语义边界分块。

        直接从原文按位置截取，保留换行/段落等格式。
        使用 ResolvedCharLimits 中的尺寸限制（已根据语言比例调整）。

        返回: [(chunk_text, start_char, end_char), ...]
        """
        if not sentences:
            return [(text, 0, len(text))] if text else []

        boundaries = await self.detect_semantic_boundaries(sentences)
        sentence_positions = self._get_sentence_positions(text, sentences)

        lim = self.limits  # 使用解析后的字符限制

        chunks = []
        start_idx = 0
        boundary_idx = 0

        while start_idx < len(sentences):
            if boundary_idx < len(boundaries):
                end_idx = boundaries[boundary_idx]
                boundary_idx += 1
            else:
                end_idx = len(sentences)

            # 从原文中按位置截取
            if start_idx < len(sentence_positions) and end_idx - 1 < len(sentence_positions):
                start_char = sentence_positions[start_idx][0]
                end_char = sentence_positions[end_idx - 1][1]
            elif start_idx < len(sentence_positions):
                start_char = sentence_positions[start_idx][0]
                end_char = len(text)
            else:
                start_char = 0
                end_char = len(text)

            chunk_text = text[start_char:end_char]

            # 太小则合并到上一块 — 使用 Token 感知的限制
            if len(chunk_text) < lim.min_semantic_chunk and chunks:
                prev_text, prev_start, prev_end = chunks[-1]
                merged_text = text[prev_start:end_char]
                if len(merged_text) <= lim.max_semantic_chunk:
                    chunks[-1] = (merged_text, prev_start, end_char)
                    start_idx = end_idx
                    continue

            # 太大则进一步切分 — 使用 Token 感知的限制
            if len(chunk_text) > lim.max_semantic_chunk:
                sub_chunks = self._split_large_chunk(chunk_text, start_char)
                chunks.extend(sub_chunks)
            else:
                if chunk_text.strip():
                    chunks.append((chunk_text, start_char, end_char))

            start_idx = end_idx

        return chunks

    def _get_sentence_positions(
        self,
        text: str,
        sentences: List[str]
    ) -> List[Tuple[int, int]]:
        """获取每个句子在原文中的位置（带鲁棒回退）"""
        positions = []
        current_pos = 0

        for sentence in sentences:
            idx = text.find(sentence, current_pos)
            if idx != -1:
                positions.append((idx, idx + len(sentence)))
                current_pos = idx + len(sentence)
                continue

            stripped = sentence.strip()
            if stripped:
                idx = text.find(stripped, current_pos)
                if idx != -1:
                    positions.append((idx, idx + len(stripped)))
                    current_pos = idx + len(stripped)
                    continue

            search_pos = current_pos
            while search_pos < len(text) and text[search_pos] in ' \t\n\r':
                search_pos += 1

            if stripped and search_pos < len(text):
                idx = text.find(stripped, search_pos)
                if idx != -1 and idx - search_pos < 200:
                    positions.append((idx, idx + len(stripped)))
                    current_pos = idx + len(stripped)
                    continue

            positions.append((current_pos, current_pos + len(sentence)))
            current_pos += len(sentence)

        return positions

    def _split_large_chunk(
        self,
        text: str,
        offset: int
    ) -> List[Tuple[str, int, int]]:
        """切分过大的块 — 使用 Token 感知的尺寸"""
        lim = self.limits
        chunks = []
        chunk_size = lim.base_chunk_size
        overlap = lim.chunk_overlap

        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))

            if end < len(text):
                for sep in ['。', '！', '？', '. ', '! ', '? ', '\n']:
                    last_sep = text.rfind(sep, start, end)
                    if last_sep > start + chunk_size // 2:
                        end = last_sep + len(sep)
                        break

            chunk = text[start:end]
            chunks.append((chunk, offset + start, offset + end))

            start = end - overlap if end < len(text) else end

        return chunks
