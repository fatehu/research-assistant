"""
智能分块策略服务 - 针对科研平台定制

特性:
1. 语义分块 - 使用嵌入相似度检测语义边界
2. 层级分块 - 多层级表示（段落/章节/文档）
3. 科研文档结构识别 - 摘要、方法、结果、结论等
4. 用户自主配置 - 灵活的分块策略选择
5. Embedding 缓存 - 请求级别缓存避免重复调用（Fix 4）
6. 引用上下文保护 - preserve_citations 落地实现（Fix 11）
"""
import re
import hashlib
import numpy as np
from enum import Enum
from typing import List, Dict, Tuple, Optional, Any, Callable
from dataclasses import dataclass, field
from loguru import logger

from app.services.embedding_service import embedding_service


# ============== 配置枚举 ==============

class ChunkingStrategy(str, Enum):
    """分块策略类型"""
    FIXED = "fixed"                    # 固定大小分块（原有方式）
    SEMANTIC = "semantic"              # 语义分块
    HIERARCHICAL = "hierarchical"      # 层级分块
    ACADEMIC = "academic"              # 学术论文专用
    HYBRID = "hybrid"                  # 混合策略（推荐）


class ChunkLevel(str, Enum):
    """分块层级"""
    PARAGRAPH = "paragraph"            # 段落级（细粒度）
    SECTION = "section"                # 章节级（中粒度）
    DOCUMENT = "document"              # 文档级（粗粒度）


# ============== 数据结构 ==============

@dataclass
class ChunkConfig:
    """分块配置 - 用户可自定义"""
    # 基础配置
    strategy: ChunkingStrategy = ChunkingStrategy.HYBRID
    base_chunk_size: int = 500          # 基础块大小（字符）
    chunk_overlap: int = 50             # 块重叠大小

    # 语义分块配置
    semantic_threshold: float = 0.75    # 语义相似度阈值（低于此值认为是边界）
    min_semantic_chunk: int = 100       # 最小语义块大小
    max_semantic_chunk: int = 1500      # 最大语义块大小

    # 层级分块配置
    enable_hierarchical: bool = True    # 是否启用层级
    hierarchy_levels: List[ChunkLevel] = field(
        default_factory=lambda: [ChunkLevel.PARAGRAPH, ChunkLevel.SECTION]
    )

    # 学术文档配置
    detect_academic_structure: bool = True   # 是否检测学术结构
    preserve_citations: bool = True          # 是否保留引用上下文

    # 高级配置
    sentence_split_threshold: int = 200      # 句子级切分阈值
    use_sliding_window: bool = True          # 是否使用滑动窗口优化
    window_size: int = 5                     # 滑动窗口大小（句子数）


@dataclass
class ChunkMetadata:
    """分块元数据"""
    level: ChunkLevel                   # 层级
    section_type: Optional[str] = None  # 学术文档章节类型
    section_title: Optional[str] = None # 章节标题
    parent_id: Optional[str] = None     # 父块ID（用于层级）
    child_ids: List[str] = field(default_factory=list)  # 子块ID列表
    semantic_score: float = 0.0         # 语义连贯性得分
    position_ratio: float = 0.0         # 在文档中的位置比例
    has_citations: bool = False         # 是否包含引用
    keywords: List[str] = field(default_factory=list)   # 关键词


@dataclass
class SmartChunk:
    """智能分块结果"""
    id: str                             # 块唯一ID
    content: str                        # 块内容
    start_char: int                     # 起始字符位置
    end_char: int                       # 结束字符位置
    metadata: ChunkMetadata             # 元数据
    embedding: Optional[List[float]] = None  # 嵌入向量

    @property
    def section_type(self) -> Optional[str]:
        """便捷属性：获取章节类型"""
        return self.metadata.section_type if self.metadata else None


# [Fix 3] ChunkResult 提升为模块级 dataclass，支持 dict 兼容访问
@dataclass
class ChunkResult:
    """统一的分块结果容器"""
    chunks: List[SmartChunk] = field(default_factory=list)
    hierarchy: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    stats: Dict[str, Any] = field(default_factory=dict)
    strategy: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ChunkResult":
        return cls(
            chunks=data.get("chunks", []),
            hierarchy=data.get("hierarchy"),
            metadata=data.get("metadata", {}),
            stats=data.get("stats", {}),
            strategy=data.get("strategy", ""),
        )

    def get(self, key: str, default=None):
        """兼容 dict 访问"""
        return getattr(self, key, default)

    def __getitem__(self, key: str):
        """兼容 dict 的 [] 访问"""
        return getattr(self, key)


# [Fix 4] Embedding 调用超限异常
class EmbeddingLimitExceeded(Exception):
    """Embedding 调用次数超限，触发降级"""
    pass


# ============== 学术文档结构检测 ==============

class AcademicStructureDetector:
    """学术文档结构检测器"""

    # 常见的学术论文章节模式（中英文）
    SECTION_PATTERNS = {
        'abstract': [
            r'^#{1,2}\s*(摘要|Abstract|ABSTRACT)\s*$',
            r'^(摘要|Abstract|ABSTRACT)\s*[:：]?\s*$',
        ],
        'introduction': [
            r'^#{1,2}\s*(\d+\.?\s*)?(引言|介绍|Introduction|INTRODUCTION)\s*$',
            r'^(\d+\.?\s*)?(引言|介绍|Introduction)\s*[:：]?\s*$',
        ],
        'related_work': [
            r'^#{1,2}\s*(\d+\.?\s*)?(相关工作|Related Work|RELATED WORK|Literature Review)\s*$',
        ],
        'methodology': [
            r'^#{1,2}\s*(\d+\.?\s*)?(方法|方法论|Methodology|Method|Methods|METHODOLOGY)\s*$',
            r'^#{1,2}\s*(\d+\.?\s*)?(研究方法|Research Method)\s*$',
        ],
        'experiment': [
            r'^#{1,2}\s*(\d+\.?\s*)?(实验|Experiment|Experiments|EXPERIMENTS)\s*$',
        ],
        'results': [
            r'^#{1,2}\s*(\d+\.?\s*)?(结果|Results|RESULTS|Findings)\s*$',
            r'^#{1,2}\s*(\d+\.?\s*)?(结果与讨论|Results and Discussion)\s*$',
        ],
        'discussion': [
            r'^#{1,2}\s*(\d+\.?\s*)?(讨论|Discussion|DISCUSSION)\s*$',
        ],
        'conclusion': [
            r'^#{1,2}\s*(\d+\.?\s*)?(结论|Conclusion|Conclusions|CONCLUSION)\s*$',
            r'^#{1,2}\s*(\d+\.?\s*)?(总结|Summary)\s*$',
        ],
        'references': [
            r'^#{1,2}\s*(参考文献|References|REFERENCES|Bibliography)\s*$',
        ],
        'appendix': [
            r'^#{1,2}\s*(附录|Appendix|APPENDIX)\s*$',
        ],
    }

    # 引用模式
    CITATION_PATTERNS = [
        r'\[(\d+(?:,\s*\d+)*)\]',           # [1], [1, 2, 3]
        r'\(([A-Z][a-z]+(?:\s+(?:et\s+al\.?|and|&)\s+)?[A-Z][a-z]+,?\s*\d{4})\)',  # (Author, 2020)
        r'([A-Z][a-z]+(?:\s+et\s+al\.?)?)\s*\((\d{4})\)',  # Author (2020)
    ]

    @classmethod
    def detect_section_type(cls, text: str) -> Optional[str]:
        """检测章节类型"""
        first_line = text.split('\n')[0].strip()

        for section_type, patterns in cls.SECTION_PATTERNS.items():
            for pattern in patterns:
                if re.match(pattern, first_line, re.IGNORECASE):
                    return section_type

        return None

    @classmethod
    def extract_section_title(cls, text: str) -> Optional[str]:
        """提取章节标题"""
        lines = text.split('\n')
        for line in lines[:3]:  # 只检查前3行
            line = line.strip()
            # 检测 Markdown 标题
            if line.startswith('#'):
                return re.sub(r'^#+\s*', '', line)
            # 检测数字编号标题（使用收紧后的正则 [Fix 6]）
            if re.match(r'^(\d+\.)+\s+\S', line):
                return line
        return None

    @classmethod
    def has_citations(cls, text: str) -> bool:
        """检测是否包含引用"""
        for pattern in cls.CITATION_PATTERNS:
            if re.search(pattern, text):
                return True
        return False

    @classmethod
    def extract_citations(cls, text: str) -> List[str]:
        """提取引用"""
        citations = []
        for pattern in cls.CITATION_PATTERNS:
            matches = re.findall(pattern, text)
            if matches:
                if isinstance(matches[0], str):
                    citations.extend(matches)
                else:
                    for m in matches:
                        if isinstance(m, tuple):
                            citations.extend(list(m))
                        else:
                            citations.append(m)
        return list(set(citations))


# ============== 语义分块器 ==============

class SemanticChunker:
    """基于语义相似度的分块器"""

    def __init__(self, config: ChunkConfig, embed_fn: Optional[Callable] = None):
        self.config = config
        # [Fix 4] 支持注入缓存 embed 函数
        self._embed_fn = embed_fn or embedding_service.embed_texts

    async def detect_semantic_boundaries(
        self,
        sentences: List[str]
    ) -> List[int]:
        """
        检测语义边界
        使用滑动窗口计算相邻句子组的相似度，在相似度骤降处切分

        返回: 边界位置索引列表
        """
        if len(sentences) < 3:
            return []

        # 获取所有句子的嵌入（通过可能带缓存的 embed 函数）
        embeddings = await self._embed_fn(sentences)

        if not embeddings or len(embeddings) != len(sentences):
            logger.warning("获取句子嵌入失败，回退到固定分块")
            return []

        boundaries = []
        window_size = min(self.config.window_size, len(sentences) // 2)

        if window_size < 2:
            return []

        # 计算相邻窗口的相似度
        similarities = []
        for i in range(len(sentences) - window_size):
            # 前窗口的平均嵌入
            window1 = np.mean(embeddings[i:i + window_size], axis=0)
            # 后窗口的平均嵌入
            window2 = np.mean(embeddings[i + 1:i + 1 + window_size], axis=0)

            # 计算余弦相似度
            sim = embedding_service.cosine_similarity(
                window1.tolist(),
                window2.tolist()
            )
            similarities.append(sim)

        if not similarities:
            return []

        # 检测相似度骤降点作为边界
        mean_sim = np.mean(similarities)
        std_sim = np.std(similarities)
        threshold = max(
            self.config.semantic_threshold,
            mean_sim - 1.5 * std_sim  # 动态阈值
        )

        for i, sim in enumerate(similarities):
            if sim < threshold:
                boundary_pos = i + window_size
                # 确保边界间距合理
                if not boundaries or (boundary_pos - boundaries[-1]) >= 2:
                    boundaries.append(boundary_pos)

        return boundaries

    async def chunk_by_semantics(
        self,
        text: str,
        sentences: List[str]
    ) -> List[Tuple[str, int, int]]:
        """
        按语义边界分块

        返回: [(chunk_text, start_char, end_char), ...]
        """
        if not sentences:
            return [(text, 0, len(text))] if text else []

        # 检测语义边界
        boundaries = await self.detect_semantic_boundaries(sentences)

        # 按边界切分
        chunks = []
        sentence_positions = self._get_sentence_positions(text, sentences)

        start_idx = 0
        boundary_idx = 0

        while start_idx < len(sentences):
            # 确定当前块的结束位置
            if boundary_idx < len(boundaries):
                end_idx = boundaries[boundary_idx]
                boundary_idx += 1
            else:
                end_idx = len(sentences)

            # 获取当前块的内容
            chunk_sentences = sentences[start_idx:end_idx]
            chunk_text = ' '.join(chunk_sentences)

            # 检查块大小是否合适
            if len(chunk_text) < self.config.min_semantic_chunk and chunks:
                # 太小，合并到上一块
                prev_chunk = chunks[-1]
                merged_text = prev_chunk[0] + '\n' + chunk_text
                if len(merged_text) <= self.config.max_semantic_chunk:
                    chunks[-1] = (merged_text, prev_chunk[1], sentence_positions[end_idx - 1][1] if end_idx <= len(sentence_positions) else len(text))
                    start_idx = end_idx
                    continue

            if len(chunk_text) > self.config.max_semantic_chunk:
                # 太大，进一步切分
                sub_chunks = self._split_large_chunk(
                    chunk_text,
                    sentence_positions[start_idx][0] if start_idx < len(sentence_positions) else 0
                )
                chunks.extend(sub_chunks)
            else:
                start_char = sentence_positions[start_idx][0] if start_idx < len(sentence_positions) else 0
                end_char = sentence_positions[end_idx - 1][1] if end_idx <= len(sentence_positions) else len(text)
                chunks.append((chunk_text, start_char, end_char))

            start_idx = end_idx

        # [Fix 11] 引用上下文保护：如果启用了 preserve_citations，扩展包含引用的块
        if self.config.preserve_citations:
            chunks = self._expand_citation_context(chunks, text, sentences)

        return chunks

    def _expand_citation_context(
        self,
        chunks: List[Tuple[str, int, int]],
        text: str,
        sentences: List[str]
    ) -> List[Tuple[str, int, int]]:
        """[Fix 11] 对包含引用的块扩展上下文，避免引用被截断"""
        expanded = []
        for chunk_text, start, end in chunks:
            if AcademicStructureDetector.has_citations(chunk_text):
                # 向前扩展到上一个句号
                new_start = text.rfind('.', 0, start)
                if new_start == -1:
                    new_start = start
                else:
                    new_start = max(0, new_start)
                # 向后扩展到下一个句号
                new_end = text.find('.', end)
                if new_end == -1 or new_end > end + 500:
                    new_end = end
                else:
                    new_end += 1

                if new_end - new_start <= self.config.max_semantic_chunk:
                    expanded.append((text[new_start:new_end], new_start, new_end))
                else:
                    expanded.append((chunk_text, start, end))
            else:
                expanded.append((chunk_text, start, end))
        return expanded

    def _get_sentence_positions(
        self,
        text: str,
        sentences: List[str]
    ) -> List[Tuple[int, int]]:
        """获取每个句子在原文中的位置"""
        positions = []
        current_pos = 0

        for sentence in sentences:
            # 查找句子在文本中的位置
            idx = text.find(sentence, current_pos)
            if idx != -1:
                positions.append((idx, idx + len(sentence)))
                current_pos = idx + len(sentence)
            else:
                # 找不到完全匹配，使用近似位置
                positions.append((current_pos, current_pos + len(sentence)))
                current_pos += len(sentence)

        return positions

    def _split_large_chunk(
        self,
        text: str,
        offset: int
    ) -> List[Tuple[str, int, int]]:
        """切分过大的块"""
        chunks = []
        chunk_size = self.config.base_chunk_size
        overlap = self.config.chunk_overlap

        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))

            # 尝试在句子边界切分
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


# ============== 层级分块器 ==============

class HierarchicalChunker:
    """层级分块器 - 创建多层级的分块表示"""

    def __init__(self, config: ChunkConfig):
        self.config = config

    def create_hierarchy(
        self,
        text: str,
        base_chunks: List[Tuple[str, int, int]]
    ) -> Dict[ChunkLevel, List[SmartChunk]]:
        """
        创建层级分块结构

        返回: {level: [SmartChunk, ...]}
        """
        hierarchy = {}
        text_length = len(text)

        # 1. 段落级（细粒度）- 使用基础分块
        if ChunkLevel.PARAGRAPH in self.config.hierarchy_levels:
            # [Fix 5] 传入文档总长度
            paragraph_chunks = self._create_paragraph_chunks(base_chunks, doc_length=text_length)
            hierarchy[ChunkLevel.PARAGRAPH] = paragraph_chunks

        # 2. 章节级（中粒度）- 合并相关段落
        if ChunkLevel.SECTION in self.config.hierarchy_levels:
            section_chunks = self._create_section_chunks(text, base_chunks)
            hierarchy[ChunkLevel.SECTION] = section_chunks

            # 建立父子关系
            if ChunkLevel.PARAGRAPH in hierarchy:
                self._link_parent_child(
                    hierarchy[ChunkLevel.SECTION],
                    hierarchy[ChunkLevel.PARAGRAPH]
                )

        # 3. 文档级（粗粒度）- 整文档摘要
        if ChunkLevel.DOCUMENT in self.config.hierarchy_levels:
            doc_chunk = self._create_document_chunk(text)
            hierarchy[ChunkLevel.DOCUMENT] = [doc_chunk]

            # 建立与章节的父子关系
            if ChunkLevel.SECTION in hierarchy:
                self._link_parent_child(
                    [doc_chunk],
                    hierarchy[ChunkLevel.SECTION]
                )

        return hierarchy

    def _create_paragraph_chunks(
        self,
        base_chunks: List[Tuple[str, int, int]],
        doc_length: int = 0
    ) -> List[SmartChunk]:
        """创建段落级分块"""
        chunks = []
        # [Fix 5] 使用文档总长度计算 position_ratio
        effective_length = max(doc_length, 1)
        for i, (content, start, end) in enumerate(base_chunks):
            chunk_id = self._generate_chunk_id(content, start)

            metadata = ChunkMetadata(
                level=ChunkLevel.PARAGRAPH,
                section_type=AcademicStructureDetector.detect_section_type(content),
                has_citations=AcademicStructureDetector.has_citations(content),
                position_ratio=round(start / effective_length, 4)  # [Fix 5] 正确计算
            )

            chunk = SmartChunk(
                id=chunk_id,
                content=content,
                start_char=start,
                end_char=end,
                metadata=metadata
            )
            chunks.append(chunk)

        return chunks

    def _create_section_chunks(
        self,
        text: str,
        base_chunks: List[Tuple[str, int, int]]
    ) -> List[SmartChunk]:
        """创建章节级分块"""
        # 检测章节边界
        section_boundaries = self._detect_section_boundaries(text)

        if not section_boundaries:
            # 没有明确的章节结构，每3-5个基础块合并为一个章节
            return self._merge_to_sections(base_chunks)

        # 按章节边界划分
        sections = []
        for i, (title, start, end, section_type) in enumerate(section_boundaries):
            section_content = text[start:end]
            # 检查章节大小，如果过大则强制切分
            # [Fix 9] 统一使用模块级 _enforce_limit 函数，消除重复代码
            if len(section_content) > 2000:
                sub_parts = _enforce_limit(section_content, 2000)

                for j, (sub_content, sub_start, sub_end) in enumerate(sub_parts):
                    abs_start = start + sub_start
                    abs_end = start + sub_end
                    chunk_id = self._generate_chunk_id(sub_content, abs_start)

                    metadata = ChunkMetadata(
                        level=ChunkLevel.SECTION,
                        section_type=section_type,
                        section_title=f"{title} (Part {j+1}/{len(sub_parts)})",
                        has_citations=AcademicStructureDetector.has_citations(sub_content),
                        position_ratio=round(abs_start / max(len(text), 1), 4)
                    )

                    chunk = SmartChunk(
                        id=chunk_id,
                        content=sub_content,
                        start_char=abs_start,
                        end_char=abs_end,
                        metadata=metadata
                    )
                    sections.append(chunk)
            else:
                chunk_id = self._generate_chunk_id(section_content, start)
                metadata = ChunkMetadata(
                    level=ChunkLevel.SECTION,
                    section_type=section_type,
                    section_title=title,
                    has_citations=AcademicStructureDetector.has_citations(section_content),
                    position_ratio=round(start / max(len(text), 1), 4)
                )
                chunk = SmartChunk(
                    id=chunk_id,
                    content=section_content,
                    start_char=start,
                    end_char=end,
                    metadata=metadata
                )
                sections.append(chunk)

        return sections

    def _detect_section_boundaries(
        self,
        text: str
    ) -> List[Tuple[str, int, int, Optional[str]]]:
        """检测章节边界"""
        boundaries = []

        # 查找所有章节标题
        lines = text.split('\n')
        current_pos = 0

        for i, line in enumerate(lines):
            stripped = line.strip()

            # [Fix 12] 先过滤 OCR 噪声，再判断标题
            if self._is_ocr_noise(stripped):
                current_pos += len(line) + 1
                continue

            # [Fix 6] 收紧编号标题正则，要求更典型的学术编号格式
            is_heading = (
                stripped.startswith('#') or
                # 严格匹配学术编号: "1.", "1.1", "3.5." 等
                re.match(r'^(\d+\.)+\s+[A-Z\u4e00-\u9fff]', stripped) is not None or
                # 中文章节编号: "第一章", "第二节" 等
                re.match(r'^第[一二三四五六七八九十百]+[章节部分]\s', stripped) is not None
            )

            # [Fix 6] 额外上下文验证：编号标题前通常有空行
            if is_heading and not stripped.startswith('#'):
                prev_line = lines[i - 1].strip() if i > 0 else ""
                if prev_line and not prev_line.startswith('#'):
                    # 编号行前面不是空行也不是标题，可能是正文中的编号
                    is_heading = False

            if is_heading:
                section_type = AcademicStructureDetector.detect_section_type(line)
                title = AcademicStructureDetector.extract_section_title(line) or stripped

                # 记录边界
                boundaries.append((title, current_pos, -1, section_type))

            current_pos += len(line) + 1  # +1 换行符

        # 填充结束位置
        for i in range(len(boundaries)):
            if i < len(boundaries) - 1:
                boundaries[i] = (
                    boundaries[i][0],
                    boundaries[i][1],
                    boundaries[i + 1][1],
                    boundaries[i][3]
                )
            else:
                boundaries[i] = (
                    boundaries[i][0],
                    boundaries[i][1],
                    len(text),
                    boundaries[i][3]
                )

        return boundaries

    @staticmethod
    def _is_ocr_noise(line: str) -> bool:
        """
        [Fix 12] 检测 OCR 噪声行，避免将图表/表格 OCR 文本误识别为章节标题。

        过滤规则:
        1. 太短 (<5 字符) 或太长 (>200 字符) 的行不适合做标题
        2. 匹配 Figure/Table 标注模式
        3. 非字母比例过高（含大量数字、符号 → 可能是 OCR 噪声）
        4. 连续大写缩写过多（如 "SAM VITDET 80M local attentionConv..."）
        5. 包含典型 OCR 噪声模式（如 "......" 省略号、连续拼接单词）
        """
        if not line:
            return False

        # 规则 1: 长度过滤（Markdown # 标题不受此限）
        if not line.startswith('#'):
            if len(line) < 5 or len(line) > 200:
                return True

        # 规则 2: Figure/Table 标注
        if re.match(
            r'^(Figure|Fig\.?|Table|Tab\.?|图|表)\s*[\d.:]+',
            line, re.IGNORECASE
        ):
            return True

        # 规则 3: 非字母/汉字比例过高 → OCR 噪声
        alpha_chars = sum(1 for c in line if c.isalpha() or '\u4e00' <= c <= '\u9fff')
        if len(line) > 10 and alpha_chars / len(line) < 0.4:
            return True

        # 规则 4: 连续大写缩写词过多（>3 个连续全大写单词，如 "SAM VITDET VIT MOE"）
        words = line.split()
        consecutive_upper = 0
        max_consecutive_upper = 0
        for w in words:
            if w.isupper() and len(w) >= 2:
                consecutive_upper += 1
                max_consecutive_upper = max(max_consecutive_upper, consecutive_upper)
            else:
                consecutive_upper = 0
        if max_consecutive_upper > 3:
            return True

        # 规则 5: 包含典型 OCR 噪声特征
        # - 连续省略号
        if '......' in line or '…' in line:
            return True
        # - 单词拼接（如 "attentionConv", "patchesvision", "tokensn/16"）
        camel_or_concat = re.findall(r'[a-z][A-Z][a-z]', line)
        if len(camel_or_concat) >= 3:
            return True
        # - 过多斜杠/管道分隔（如 "n/16 DeepEncoderDeepSeek -3B (MOE -A570M)"）
        if line.count('/') + line.count('|') >= 4:
            return True

        return False

    def _merge_to_sections(
        self,
        base_chunks: List[Tuple[str, int, int]],
        max_section_chars: int = 3000
    ) -> List[SmartChunk]:
        """
        [Fix 12] 将基础块合并为章节 — 内容感知版本。
        不再盲目每 N 块一组，而是:
        1. 优先在自然段落/空行边界处切分
        2. 尝试从首块中提取标题作为 section_title
        3. 控制最大章节大小
        """
        if not base_chunks:
            return []

        sections = []
        current_group: List[Tuple[str, int, int]] = []
        current_size = 0

        for chunk in base_chunks:
            chunk_text, chunk_start, chunk_end = chunk
            chunk_len = len(chunk_text)

            # 检测是否应该开启新章节
            should_break = False

            if current_group:
                # 条件 1: 累积大小超过上限
                if current_size + chunk_len > max_section_chars:
                    should_break = True

                # 条件 2: 当前块以明显的段落标题开头（编号、Markdown标题等）
                first_line = chunk_text.split('\n')[0].strip()
                if (first_line.startswith('#') or
                    re.match(r'^(\d+\.)+\s+[A-Z\u4e00-\u9fff]', first_line) or
                    re.match(r'^第[一二三四五六七八九十百]+[章节部分]\s', first_line)):
                    should_break = True

                # 条件 3: 前一块末尾和当前块开头存在双换行（段落分隔）
                prev_text = current_group[-1][0]
                if prev_text.rstrip().endswith('\n') or chunk_text.lstrip().startswith('\n'):
                    # 如果已经积累了足够的内容，在段落边界切分
                    if current_size >= max_section_chars * 0.5:
                        should_break = True

            if should_break and current_group:
                sections.append(self._build_section_chunk(current_group))
                current_group = []
                current_size = 0

            current_group.append(chunk)
            current_size += chunk_len

        # 处理最后一组
        if current_group:
            sections.append(self._build_section_chunk(current_group))

        return sections

    def _build_section_chunk(
        self,
        group: List[Tuple[str, int, int]]
    ) -> SmartChunk:
        """[Fix 12] 从一组基础块构建章节级 SmartChunk"""
        content = '\n\n'.join(chunk[0] for chunk in group)
        start = group[0][1]
        end = group[-1][2]

        # 尝试从第一个块提取标题
        first_text = group[0][0]
        section_title = AcademicStructureDetector.extract_section_title(first_text)
        section_type = AcademicStructureDetector.detect_section_type(first_text)

        chunk_id = self._generate_chunk_id(content, start)

        metadata = ChunkMetadata(
            level=ChunkLevel.SECTION,
            section_type=section_type,
            section_title=section_title,
            has_citations=AcademicStructureDetector.has_citations(content)
        )

        return SmartChunk(
            id=chunk_id,
            content=content,
            start_char=start,
            end_char=end,
            metadata=metadata
        )

    def _create_document_chunk(self, text: str) -> SmartChunk:
        """创建文档级分块（摘要）"""
        # 提取文档摘要（取前1000字符或摘要章节）
        summary = self._extract_document_summary(text)

        chunk_id = self._generate_chunk_id(text, 0)

        metadata = ChunkMetadata(
            level=ChunkLevel.DOCUMENT,
            has_citations=AcademicStructureDetector.has_citations(text)
        )

        return SmartChunk(
            id=chunk_id,
            content=summary,
            start_char=0,
            end_char=len(text),
            metadata=metadata
        )

    def _extract_document_summary(self, text: str, max_length: int = 1500) -> str:
        """提取文档摘要"""
        # 尝试找到摘要章节
        abstract_patterns = [
            r'(?:^|\n)#{1,2}\s*(?:摘要|Abstract)\s*\n([\s\S]*?)(?=\n#{1,2}|\Z)',
            r'(?:摘要|Abstract)\s*[:：]\s*([\s\S]{100,}?)(?=\n\n|\n#{1,2}|\Z)',
        ]

        for pattern in abstract_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                abstract = match.group(1).strip()
                if len(abstract) > 50:
                    return abstract[:max_length]

        # 没有找到摘要，返回前N个字符
        return text[:max_length]

    def _link_parent_child(
        self,
        parents: List[SmartChunk],
        children: List[SmartChunk]
    ):
        """建立父子关系"""
        for parent in parents:
            parent.metadata.child_ids = []

            for child in children:
                # 检查子块是否在父块范围内
                if (child.start_char >= parent.start_char and
                    child.end_char <= parent.end_char):
                    parent.metadata.child_ids.append(child.id)
                    child.metadata.parent_id = parent.id

    @staticmethod
    def _generate_chunk_id(content: str, position: int) -> str:
        """[Fix 10] 生成块ID — 使用 SHA-256 前 16 位，加入内容长度降低碰撞"""
        hash_input = f"{content[:200]}|{position}|{len(content)}"
        return hashlib.sha256(hash_input.encode()).hexdigest()[:16]


# ============== 模块级工具函数 ==============

# [Fix 9] 统一的递归切分函数（消除 DRY 违规）
def _enforce_limit(text: str, max_chars: int = 2000) -> List[Tuple[str, int, int]]:
    """
    强制限制块大小，递归切分过大的块
    返回: [(text, start_offset, end_offset), ...] 相对于输入文本起始位置 0
    """
    if len(text) <= max_chars:
        return [(text, 0, len(text))]

    # 尝试在段落/句子边界切分
    mid = len(text) // 2
    # 寻找中间附近的最佳切分点
    split_pos = -1

    # 优先级: 换行 > 句号 > 逗号 > 空格
    for sep in ['\n\n', '\n', '。', '.', '；', ';', '，', ',', ' ']:
        # 在中间区域搜索 (mid - 20%, mid + 20%)
        search_range = int(len(text) * 0.2)
        start_search = max(0, mid - search_range)
        end_search = min(len(text), mid + search_range)

        check_pos = text.rfind(sep, start_search, end_search)
        if check_pos != -1:
            split_pos = check_pos + len(sep)
            break

    # 如果找不到合适的切分点，强制在中间切分
    if split_pos == -1:
        split_pos = mid

    first_half = text[:split_pos]
    second_half = text[split_pos:]

    chunks = []
    # 递归处理前一半
    for c_text, c_start, c_end in _enforce_limit(first_half, max_chars):
        chunks.append((c_text, c_start, c_end))

    # 递归处理后一半
    for c_text, c_start, c_end in _enforce_limit(second_half, max_chars):
        chunks.append((c_text, split_pos + c_start, split_pos + c_end))

    return chunks


# ============== 主服务类 ==============

class SmartChunkingService:
    """
    智能分块服务 - 统一入口

    使用示例:
    ```python
    service = SmartChunkingService()

    # 异步调用（推荐，适用于 FastAPI 等异步环境）
    result = await service.chunk_document(text)

    # 同步调用（仅限脚本/CLI 环境）
    result = service.chunk(text, config)
    ```
    """

    # [Fix 4] 每次 chunk_document 调用的最大 embedding 请求次数
    MAX_EMBEDDING_CALLS = 20

    def __init__(self):
        self.semantic_chunker = None
        self.hierarchical_chunker = None
        # [Fix 4] 请求级 embedding 缓存
        self._embedding_cache: Dict[str, List[float]] = {}
        self._embedding_call_count: int = 0

    # [Fix 3] 安全化同步包装器
    def chunk(self, text: str, config: Optional[ChunkConfig] = None) -> ChunkResult:
        """
        同步接口 - 仅限脚本/CLI 环境使用。
        在 FastAPI 等异步框架中请直接调用 chunk_document()。
        """
        import asyncio

        try:
            loop = asyncio.get_running_loop()
            # 如果事件循环正在运行，说明在异步环境中
            raise RuntimeError(
                "检测到正在运行的事件循环。请使用 `await service.chunk_document()` 替代。"
                "同步 chunk() 仅限脚本环境。"
            )
        except RuntimeError as e:
            if "no running event loop" not in str(e).lower() and "no current event loop" not in str(e).lower():
                raise

        # 确认没有运行中的 loop，安全使用 asyncio.run()
        return asyncio.run(self.chunk_document(text, config))

    # [Fix 7] 增强 analyze_document 方法
    def analyze_document(self, text: str) -> Dict[str, Any]:
        """分析文档结构，返回完整的文档特征和策略推荐"""
        is_academic = self._detect_academic_document(text)

        # 检测章节结构
        temp_chunker = HierarchicalChunker(ChunkConfig())
        section_boundaries = temp_chunker._detect_section_boundaries(text)

        detected_sections = []
        for title, start, end, section_type in section_boundaries:
            detected_sections.append({
                "title": title,
                "type": section_type,
                "start": start,
                "end": end,
                "length": end - start,
            })

        # 推荐策略
        if is_academic:
            recommended_strategy = "academic"
            recommended_reason = "检测到学术文档结构，推荐使用学术论文专用分块"
        elif len(detected_sections) >= 3:
            recommended_strategy = "hierarchical"
            recommended_reason = "检测到多个章节，推荐使用层级分块"
        elif len(text) > 10000:
            recommended_strategy = "semantic"
            recommended_reason = "长文档，推荐使用语义分块以获得更好的边界"
        else:
            recommended_strategy = "hybrid"
            recommended_reason = "通用文档，推荐使用混合策略"

        sentences = self._split_to_sentences(text)

        return {
            "is_academic": is_academic,
            "detected_sections": detected_sections,
            "has_citations": AcademicStructureDetector.has_citations(text),
            "recommended_strategy": recommended_strategy,
            "recommended_reason": recommended_reason,
            "document_stats": {
                "total_chars": len(text),
                "total_sentences": len(sentences),
                "total_paragraphs": text.count('\n\n') + 1,
                "avg_sentence_length": len(text) // max(len(sentences), 1),
                "section_count": len(detected_sections),
            },
            "estimated_chunks": len(text) // 500 + 1,
            "language": "zh" if any('\u4e00' <= c <= '\u9fff' for c in text[:1000]) else "en",
        }

    def get_preset_configs(self) -> Dict[str, ChunkConfig]:
        """获取所有可用的预设配置对象"""
        return {
            "default": get_preset_config("default"),
            "fast": get_preset_config("fast"),
            "precise": get_preset_config("precise"),
            "academic": get_preset_config("academic"),
            "deep": get_preset_config("deep"),
        }

    async def chunk_document(
        self,
        text: str,
        config: Optional[ChunkConfig] = None,
        file_type: str = "txt"
    ) -> ChunkResult:
        """
        对文档进行智能分块

        参数:
            text: 文档文本
            config: 分块配置（可选）
            file_type: 文件类型

        返回:
            ChunkResult 数据类
        """
        if not text:
            return self._empty_result()

        # 使用默认配置或用户提供的配置
        config = config or ChunkConfig()

        # [Fix 4] 重置请求级缓存
        self._embedding_cache = {}
        self._embedding_call_count = 0

        # 初始化分块器（注入缓存 embed 函数）
        self.semantic_chunker = SemanticChunker(config, embed_fn=self._cached_embed_texts)
        self.hierarchical_chunker = HierarchicalChunker(config)

        # 预处理文本
        text = self._preprocess_text(text)

        # 根据策略选择分块方法
        try:
            if config.strategy == ChunkingStrategy.FIXED:
                result = await self._fixed_chunking(text, config)
            elif config.strategy == ChunkingStrategy.SEMANTIC:
                result = await self._semantic_chunking(text, config)
            elif config.strategy == ChunkingStrategy.HIERARCHICAL:
                result = await self._hierarchical_chunking(text, config)
            elif config.strategy == ChunkingStrategy.ACADEMIC:
                result = await self._academic_chunking(text, config)
            else:  # HYBRID（默认）
                result = await self._hybrid_chunking(text, config)
        except EmbeddingLimitExceeded:
            # [Fix 4] Embedding 超限时降级到固定分块
            logger.warning("Embedding 调用次数超限，降级到固定分块")
            result = await self._fixed_chunking(text, config)

        # [Fix 2] 统一 normalize 返回结构
        result = self._normalize_result(result, text)

        # 构建 ChunkResult
        chunks = result.get("chunks", [])
        return ChunkResult(
            strategy=config.strategy.value,
            chunks=chunks,
            hierarchy=result.get("hierarchy"),
            metadata=result.get("metadata", {}),
            stats=self._calculate_stats(chunks, text)
        )

    # ===== [Fix 4] Embedding 缓存层 =====

    async def _cached_embed_texts(self, texts: List[str]) -> List[List[float]]:
        """带缓存的 embedding 调用，避免同一文档内重复请求"""
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
                logger.warning(f"Embedding 调用次数已达上限 {self.MAX_EMBEDDING_CALLS}，触发降级")
                raise EmbeddingLimitExceeded()

            self._embedding_call_count += 1
            embeddings = await embedding_service.embed_texts(texts_to_embed)

            for j, idx in enumerate(indices_to_embed):
                cache_key = hashlib.md5(texts_to_embed[j].encode()).hexdigest()
                self._embedding_cache[cache_key] = embeddings[j]
                results[idx] = embeddings[j]

        return results

    # ===== [Fix 2] 统一 normalize 返回结构 =====

    def _normalize_result(self, result: Dict[str, Any], text: str) -> Dict[str, Any]:
        """
        统一结果数据结构，确保：
        - chunks: List[SmartChunk]（始终是 SmartChunk 对象列表）
        - hierarchy: Optional[Dict[str, List[Dict]]]（键为字符串，值为字典列表）
        - metadata: Dict[str, Any]
        """
        # 1. 确保 chunks 都是 SmartChunk 对象
        normalized_chunks = []
        for chunk in result.get("chunks", []):
            if isinstance(chunk, SmartChunk):
                normalized_chunks.append(chunk)
            elif isinstance(chunk, dict):
                # 从 dict 重建 SmartChunk
                meta = chunk.get("metadata", {})
                level_val = meta.get("level", "paragraph")
                try:
                    level = ChunkLevel(level_val)
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

        # 2. 确保 hierarchy 格式统一: Dict[str, List[Dict]]
        hierarchy = result.get("hierarchy")
        if hierarchy is not None:
            normalized_hierarchy = {}
            for key, chunks in hierarchy.items():
                # 键统一为字符串
                str_key = key.value if isinstance(key, ChunkLevel) else str(key)
                # 值统一为 dict 列表
                normalized_hierarchy[str_key] = [
                    self._chunk_to_dict(c) if isinstance(c, SmartChunk) else c
                    for c in chunks
                ]
            result["hierarchy"] = normalized_hierarchy

        # 3. [Fix 5] 为所有 chunk 补充 position_ratio
        text_len = max(len(text), 1)
        for chunk in result["chunks"]:
            if chunk.metadata.position_ratio == 0.0 and chunk.start_char > 0:
                chunk.metadata.position_ratio = round(chunk.start_char / text_len, 4)

        return result

    # ===== 各策略实现 =====

    async def _fixed_chunking(
        self,
        text: str,
        config: ChunkConfig
    ) -> Dict[str, Any]:
        """固定大小分块（兼容原有方式）"""
        from app.services.document_service import TextSplitter

        splitter = TextSplitter(
            chunk_size=config.base_chunk_size,
            chunk_overlap=config.chunk_overlap
        )

        raw_chunks = splitter.split_text(text)

        chunks = []
        for i, (content, start, end) in enumerate(raw_chunks):
            chunk_id = self.hierarchical_chunker._generate_chunk_id(content, start)

            chunk = SmartChunk(
                id=chunk_id,
                content=content,
                start_char=start,
                end_char=end,
                metadata=ChunkMetadata(level=ChunkLevel.PARAGRAPH)
            )
            chunks.append(chunk)

        return {
            "chunks": chunks,
            "hierarchy": None,
            "metadata": {}
        }

    async def _semantic_chunking(
        self,
        text: str,
        config: ChunkConfig
    ) -> Dict[str, Any]:
        """语义分块 - 带 embedding 降级保护"""
        # 分句
        sentences = self._split_to_sentences(text)

        # 尝试语义分块，失败时降级到固定分块
        try:
            raw_chunks = await self.semantic_chunker.chunk_by_semantics(text, sentences)
        except EmbeddingLimitExceeded:
            raise  # 让上层处理降级
        except Exception as e:
            logger.warning(f"语义分块失败，降级到固定分块: {e}")
            return await self._fixed_chunking(text, config)

        # 如果语义分块只返回一个大块，也降级
        if len(raw_chunks) <= 1 and len(text) > config.base_chunk_size * 2:
            logger.info("语义分块结果不理想（仅1块），降级到固定分块")
            return await self._fixed_chunking(text, config)

        # 转换为 SmartChunk
        chunks = []
        for content, start, end in raw_chunks:
            chunk_id = self.hierarchical_chunker._generate_chunk_id(content, start)

            metadata = ChunkMetadata(
                level=ChunkLevel.PARAGRAPH,
                section_type=AcademicStructureDetector.detect_section_type(content),
                has_citations=AcademicStructureDetector.has_citations(content)
            )

            chunk = SmartChunk(
                id=chunk_id,
                content=content,
                start_char=start,
                end_char=end,
                metadata=metadata
            )
            chunks.append(chunk)

        return {
            "chunks": chunks,
            "hierarchy": None,
            "metadata": {}
        }

    async def _hierarchical_chunking(
        self,
        text: str,
        config: ChunkConfig
    ) -> Dict[str, Any]:
        """层级分块"""
        # 先进行语义分块作为基础，失败时使用固定分块
        try:
            sentences = self._split_to_sentences(text)
            raw_chunks = await self.semantic_chunker.chunk_by_semantics(text, sentences)
        except (EmbeddingLimitExceeded, Exception) as e:
            logger.warning(f"层级分块中语义分块受限或失败，使用固定分块: {e}")
            from app.services.document_service import TextSplitter
            splitter = TextSplitter(chunk_size=config.base_chunk_size, chunk_overlap=config.chunk_overlap)
            raw_chunks = splitter.split_text(text)

        # 创建层级结构
        hierarchy = self.hierarchical_chunker.create_hierarchy(text, raw_chunks)

        # 主分块使用段落级
        main_chunks = hierarchy.get(ChunkLevel.PARAGRAPH, [])

        return {
            "chunks": main_chunks,
            "hierarchy": {
                level.value: [self._chunk_to_dict(c) for c in chunks]
                for level, chunks in hierarchy.items()
            },
            "metadata": {}
        }

    async def _academic_chunking(
        self,
        text: str,
        config: ChunkConfig
    ) -> Dict[str, Any]:
        """学术论文专用分块"""
        # 检测学术结构
        section_boundaries = self.hierarchical_chunker._detect_section_boundaries(text)

        chunks = []
        hierarchy = {ChunkLevel.SECTION.value: [], ChunkLevel.PARAGRAPH.value: []}

        for title, start, end, section_type in section_boundaries:
            section_text = text[start:end]

            # 创建章节级分块
            # [Fix 9] 统一使用模块级 _enforce_limit 函数
            section_parts = _enforce_limit(section_text, 2000)

            for i, (part_content, part_start, part_end) in enumerate(section_parts):
                abs_start = start + part_start
                abs_end = start + part_end
                part_id = self.hierarchical_chunker._generate_chunk_id(part_content, abs_start)

                section_chunk = SmartChunk(
                    id=part_id,
                    content=part_content,
                    start_char=abs_start,
                    end_char=abs_end,
                    metadata=ChunkMetadata(
                        level=ChunkLevel.SECTION,
                        section_type=section_type,
                        section_title=f"{title} (Part {i+1}/{len(section_parts)})" if len(section_parts) > 1 else title,
                        has_citations=AcademicStructureDetector.has_citations(part_content)
                    )
                )
                hierarchy[ChunkLevel.SECTION.value].append(self._chunk_to_dict(section_chunk))

                # 对该部分进行细粒度分块 (Paragraphs)
                sentences = self._split_to_sentences(part_content)
                try:
                    section_chunks = await self.semantic_chunker.chunk_by_semantics(part_content, sentences)
                except (EmbeddingLimitExceeded, Exception) as e:
                    logger.warning(f"章节内语义分块受限或失败，使用固定分块: {e}")
                    from app.services.document_service import TextSplitter
                    splitter = TextSplitter(chunk_size=config.base_chunk_size, chunk_overlap=config.chunk_overlap)
                    section_chunks = splitter.split_text(part_content)

                for content, sub_start, sub_end in section_chunks:
                    chunk_abs_start = abs_start + sub_start
                    chunk_abs_end = abs_start + sub_end
                    chunk_id = self.hierarchical_chunker._generate_chunk_id(content, chunk_abs_start)

                    chunk = SmartChunk(
                        id=chunk_id,
                        content=content,
                        start_char=chunk_abs_start,
                        end_char=chunk_abs_end,
                        metadata=ChunkMetadata(
                            level=ChunkLevel.PARAGRAPH,
                            section_type=section_type,
                            section_title=section_chunk.metadata.section_title,
                            parent_id=part_id,
                            has_citations=AcademicStructureDetector.has_citations(content)
                        )
                    )
                    chunks.append(chunk)
                    hierarchy[ChunkLevel.PARAGRAPH.value].append(self._chunk_to_dict(chunk))

        # 如果没有检测到学术结构，回退到语义分块
        if not chunks:
            return await self._semantic_chunking(text, config)

        return {
            "chunks": chunks,
            "hierarchy": hierarchy,
            "metadata": {
                "is_academic": True,
                "detected_sections": [b[3] for b in section_boundaries if b[3]]
            }
        }

    async def _hybrid_chunking(
        self,
        text: str,
        config: ChunkConfig
    ) -> Dict[str, Any]:
        """混合策略分块（推荐）"""
        # 1. 检测是否为学术文档
        is_academic = self._detect_academic_document(text)

        if is_academic and config.detect_academic_structure:
            result = await self._academic_chunking(text, config)
        else:
            # 2. 使用语义分块
            result = await self._semantic_chunking(text, config)

        # 3. 如果启用层级，创建层级结构
        if config.enable_hierarchical:
            raw_chunks = [(c.content, c.start_char, c.end_char) for c in result["chunks"]]
            hierarchy = self.hierarchical_chunker.create_hierarchy(text, raw_chunks)

            result["hierarchy"] = {
                level.value: [self._chunk_to_dict(c) for c in chunks]
                for level, chunks in hierarchy.items()
            }

        return result

    # ===== 工具方法 =====

    def _preprocess_text(self, text: str) -> str:
        """预处理文本"""
        # 统一换行符
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        # 移除多余空行
        text = re.sub(r'\n{3,}', '\n\n', text)
        # 移除多余空格
        text = re.sub(r' {2,}', ' ', text)

        # [Fix 13] 清除 PDF 提取中的图表/表格内部 OCR 噪声块
        text = self._strip_figure_noise_blocks(text)

        return text.strip()

    @staticmethod
    def _strip_figure_noise_blocks(text: str) -> str:
        """
        [Fix 13] 检测并移除 pypdf 提取的图表/表格内部噪声文本块。

        pypdf 从 PDF 中提取文本时，会把 Figure/Table 内部的标注文字
        （如架构图里的 "SAM", "VITDET", "80M", "Conv 16x" 等）
        提取为一连串短碎片行，混入正文。这些噪声块的特征:
        - 连续多行(≥3)很短的行（<60 字符）
        - 不以句号等标点结尾（不是正常句子）
        - 不匹配章节标题模式
        - 通常紧跟在 "Figure N|..." 或 "Table N|..." 标注前面

        本方法检测这类噪声块并移除，保留 Figure/Table 的 caption 正文。
        """
        lines = text.split('\n')
        cleaned_lines = []
        i = 0

        while i < len(lines):
            # 检测潜在噪声块的起始
            if SmartChunkingService._is_fragment_line(lines[i]):
                # 向前探测连续碎片行
                block_start = i
                while i < len(lines) and SmartChunkingService._is_fragment_line(lines[i]):
                    i += 1

                block_length = i - block_start

                # 只有连续 3+ 行碎片才认为是噪声块
                if block_length >= 3:
                    # 检查这个块后面是否紧跟 Figure/Table caption（强信号）
                    next_line = lines[i].strip() if i < len(lines) else ""
                    is_before_caption = bool(re.match(
                        r'^(Figure|Fig\.?|Table|Tab\.?|图|表)\s*\d',
                        next_line, re.IGNORECASE
                    ))

                    # 进一步验证：计算这个块的"噪声密度"
                    block_lines = lines[block_start:i]
                    avg_len = sum(len(l.strip()) for l in block_lines) / max(block_length, 1)
                    has_sentence = any(
                        l.strip().endswith(('.', '。', '!', '！', '?', '？'))
                        and len(l.strip()) > 30
                        for l in block_lines
                    )

                    # 判定为噪声块的条件:
                    # (a) 紧跟在 figure/table caption 前，或
                    # (b) 平均行长 < 40 且没有完整句子
                    if is_before_caption or (avg_len < 40 and not has_sentence):
                        logger.debug(
                            f"[Fix 13] 移除图表噪声块: {block_length} 行, "
                            f"avg_len={avg_len:.0f}, 内容='{block_lines[0].strip()[:50]}...'"
                        )
                        # 跳过这个噪声块，不加入 cleaned_lines
                        continue
                    else:
                        # 不满足噪声条件，保留原始行
                        cleaned_lines.extend(block_lines)
                        continue
                else:
                    # 只有 1-2 行碎片，保留（可能是正常短行）
                    cleaned_lines.extend(lines[block_start:i])
                    continue

            cleaned_lines.append(lines[i])
            i += 1

        return '\n'.join(cleaned_lines)

    @staticmethod
    def _is_fragment_line(line: str) -> bool:
        """
        [Fix 13] 判断一行是否为图表碎片行（pypdf 从图中提取的短文字标签）。

        碎片行特征：短、非句子、非标题、含大量缩写/数字/特殊符号。
        """
        stripped = line.strip()
        if not stripped:
            return False

        # 太长的行不是碎片
        if len(stripped) > 60:
            return False

        # 是正常的章节标题，不是碎片
        if stripped.startswith('#'):
            return False
        if re.match(r'^(\d+\.)+\s+[A-Z\u4e00-\u9fff]', stripped):
            return False
        if re.match(r'^第[一二三四五六七八九十百]+[章节部分]', stripped):
            return False

        # Figure/Table caption 行不是碎片
        if re.match(r'^(Figure|Fig\.?|Table|Tab\.?|图|表)\s*\d', stripped, re.IGNORECASE):
            return False

        # 以句号结尾且长度合理的行不是碎片
        if len(stripped) > 30 and stripped[-1] in '.。!！?？':
            return False

        # 以下特征判定为碎片:
        # 1. 非常短（< 20 字符）
        if len(stripped) < 20:
            return True

        # 2. 不以任何标点结尾
        if stripped[-1] not in '.。,，;；:：!！?？)）]】"\'':
            # 且字母比例低于 60%
            alpha_chars = sum(1 for c in stripped if c.isalpha() or '\u4e00' <= c <= '\u9fff')
            if alpha_chars / max(len(stripped), 1) < 0.6:
                return True

        return False

    def _split_to_sentences(self, text: str) -> List[str]:
        """将文本分割为句子"""
        # 中英文混合分句
        sentence_endings = r'(?<=[。！？.!?])\s*(?=[^。！？.!?\s])'
        sentences = re.split(sentence_endings, text)

        # 过滤空句子并清理
        sentences = [s.strip() for s in sentences if s.strip()]

        # 处理过长的句子
        result = []
        for sentence in sentences:
            if len(sentence) > 500:
                # 在逗号处切分长句
                sub_sentences = re.split(r'(?<=[，,;；])\s*', sentence)
                result.extend([s for s in sub_sentences if s.strip()])
            else:
                result.append(sentence)

        return result

    def _detect_academic_document(self, text: str) -> bool:
        """检测是否为学术文档"""
        academic_markers = 0

        for section_type in ['abstract', 'introduction', 'methodology', 'conclusion', 'references']:
            for pattern in AcademicStructureDetector.SECTION_PATTERNS.get(section_type, []):
                if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
                    academic_markers += 1
                    break

        # 检查是否有引用
        if AcademicStructureDetector.has_citations(text):
            academic_markers += 1

        return academic_markers >= 2

    def _chunk_to_dict(self, chunk: SmartChunk) -> Dict[str, Any]:
        """将 SmartChunk 转换为字典"""
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
            }
        }

    def _calculate_stats(
        self,
        chunks: List[SmartChunk],
        text: str
    ) -> Dict[str, Any]:
        """计算统计信息"""
        if not chunks:
            return {}

        chunk_sizes = [len(c.content) for c in chunks]

        return {
            "total_chunks": len(chunks),
            "total_chars": len(text),
            "avg_chunk_size": sum(chunk_sizes) // len(chunk_sizes),
            "min_chunk_size": min(chunk_sizes),
            "max_chunk_size": max(chunk_sizes),
            "chunks_with_citations": sum(1 for c in chunks if c.metadata.has_citations),
        }

    def _empty_result(self) -> ChunkResult:
        """返回空结果"""
        return ChunkResult(strategy="none")


# ============== 便捷函数 ==============

# 全局服务实例
smart_chunking_service = SmartChunkingService()


async def chunk_document_smart(
    text: str,
    strategy: str = "hybrid",
    **kwargs
) -> Dict[str, Any]:
    """
    便捷函数 - 智能分块文档

    参数:
        text: 文档文本
        strategy: 分块策略 (fixed/semantic/hierarchical/academic/hybrid)
        **kwargs: 其他配置参数

    示例:
        result = await chunk_document_smart(
            text,
            strategy="hybrid",
            semantic_threshold=0.7,
            enable_hierarchical=True
        )
    """
    config = ChunkConfig(
        strategy=ChunkingStrategy(strategy),
        **{k: v for k, v in kwargs.items() if hasattr(ChunkConfig, k)}
    )

    return await smart_chunking_service.chunk_document(text, config)


def get_preset_config(preset: str) -> ChunkConfig:
    """
    获取预设配置

    预设:
        - "default": 默认混合策略
        - "fast": 快速固定分块
        - "precise": 精确语义分块
        - "academic": 学术论文优化
        - "deep": 深度层级分块
    """
    presets = {
        "default": ChunkConfig(),
        "fast": ChunkConfig(
            strategy=ChunkingStrategy.FIXED,
            enable_hierarchical=False
        ),
        "precise": ChunkConfig(
            strategy=ChunkingStrategy.SEMANTIC,
            semantic_threshold=0.65,
            min_semantic_chunk=150,
            enable_hierarchical=False
        ),
        "academic": ChunkConfig(
            strategy=ChunkingStrategy.ACADEMIC,
            detect_academic_structure=True,
            preserve_citations=True,
            enable_hierarchical=True
        ),
        "deep": ChunkConfig(
            strategy=ChunkingStrategy.HIERARCHICAL,
            enable_hierarchical=True,
            hierarchy_levels=[
                ChunkLevel.PARAGRAPH,
                ChunkLevel.SECTION,
                ChunkLevel.DOCUMENT
            ]
        ),
    }

    return presets.get(preset, presets["default"])
