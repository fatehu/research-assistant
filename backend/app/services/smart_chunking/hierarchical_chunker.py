"""
智能分块 - 层级分块器

创建多层级的分块表示（段落/章节/文档），
以及大块递归切分工具函数。
"""
import re
import hashlib
from typing import List, Dict, Tuple, Optional

from .types import (
    ChunkConfig, ChunkLevel, ChunkMetadata, SmartChunk, generate_chunk_id,
)
from .academic_detector import AcademicStructureDetector


# ============== 工具函数 ==============

def enforce_limit(text: str, max_chars: int = 2000) -> List[Tuple[str, int, int]]:
    """
    强制限制块大小，递归切分过大的块。

    返回: [(text, start_offset, end_offset), ...] 相对于输入文本起始位置 0
    """
    if len(text) <= max_chars:
        return [(text, 0, len(text))]

    mid = len(text) // 2
    split_pos = -1

    for sep in ['\n\n', '\n', '。', '.', '；', ';', '，', ',', ' ']:
        search_range = int(len(text) * 0.2)
        start_search = max(0, mid - search_range)
        end_search = min(len(text), mid + search_range)

        check_pos = text.rfind(sep, start_search, end_search)
        if check_pos != -1:
            split_pos = check_pos + len(sep)
            break

    if split_pos == -1:
        split_pos = mid

    first_half = text[:split_pos]
    second_half = text[split_pos:]

    chunks = []
    for c_text, c_start, c_end in enforce_limit(first_half, max_chars):
        chunks.append((c_text, c_start, c_end))
    for c_text, c_start, c_end in enforce_limit(second_half, max_chars):
        chunks.append((c_text, split_pos + c_start, split_pos + c_end))

    return chunks


# 向后兼容别名
_enforce_limit = enforce_limit


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
        创建层级分块结构。

        返回: {level: [SmartChunk, ...]}
        """
        hierarchy = {}
        text_length = len(text)

        if ChunkLevel.PARAGRAPH in self.config.hierarchy_levels:
            paragraph_chunks = self._create_paragraph_chunks(base_chunks, doc_length=text_length)
            hierarchy[ChunkLevel.PARAGRAPH] = paragraph_chunks

        if ChunkLevel.SECTION in self.config.hierarchy_levels:
            section_chunks = self._create_section_chunks(text, base_chunks)
            hierarchy[ChunkLevel.SECTION] = section_chunks

            if ChunkLevel.PARAGRAPH in hierarchy:
                self._link_parent_child(
                    hierarchy[ChunkLevel.SECTION],
                    hierarchy[ChunkLevel.PARAGRAPH]
                )

        if ChunkLevel.DOCUMENT in self.config.hierarchy_levels:
            doc_chunk = self._create_document_chunk(text)
            hierarchy[ChunkLevel.DOCUMENT] = [doc_chunk]

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
        effective_length = max(doc_length, 1)
        for i, (content, start, end) in enumerate(base_chunks):
            chunk_id = generate_chunk_id(content, start)

            metadata = ChunkMetadata(
                level=ChunkLevel.PARAGRAPH,
                section_type=AcademicStructureDetector.detect_section_type(content),
                has_citations=AcademicStructureDetector.has_citations(content),
                position_ratio=round(start / effective_length, 4)
            )

            chunks.append(SmartChunk(
                id=chunk_id, content=content,
                start_char=start, end_char=end, metadata=metadata
            ))

        return chunks

    def _create_section_chunks(
        self,
        text: str,
        base_chunks: List[Tuple[str, int, int]]
    ) -> List[SmartChunk]:
        """创建章节级分块"""
        section_boundaries = self._detect_section_boundaries(text)

        if not section_boundaries:
            return self._merge_to_sections(base_chunks)

        sections = []
        for i, (title, start, end, section_type) in enumerate(section_boundaries):
            section_content = text[start:end]

            if len(section_content) > 2000:
                sub_parts = enforce_limit(section_content, 2000)

                for j, (sub_content, sub_start, sub_end) in enumerate(sub_parts):
                    abs_start = start + sub_start
                    abs_end = start + sub_end
                    chunk_id = generate_chunk_id(sub_content, abs_start)

                    metadata = ChunkMetadata(
                        level=ChunkLevel.SECTION,
                        section_type=section_type,
                        section_title=f"{title} (Part {j+1}/{len(sub_parts)})",
                        has_citations=AcademicStructureDetector.has_citations(sub_content),
                        position_ratio=round(abs_start / max(len(text), 1), 4)
                    )

                    sections.append(SmartChunk(
                        id=chunk_id, content=sub_content,
                        start_char=abs_start, end_char=abs_end, metadata=metadata
                    ))
            else:
                chunk_id = generate_chunk_id(section_content, start)
                metadata = ChunkMetadata(
                    level=ChunkLevel.SECTION,
                    section_type=section_type,
                    section_title=title,
                    has_citations=AcademicStructureDetector.has_citations(section_content),
                    position_ratio=round(start / max(len(text), 1), 4)
                )
                sections.append(SmartChunk(
                    id=chunk_id, content=section_content,
                    start_char=start, end_char=end, metadata=metadata
                ))

        return sections

    def _detect_section_boundaries(
        self,
        text: str
    ) -> List[Tuple[str, int, int, Optional[str]]]:
        """检测章节边界"""
        boundaries = []
        lines = text.split('\n')
        current_pos = 0

        for i, line in enumerate(lines):
            stripped = line.strip()

            if self._is_ocr_noise(stripped):
                current_pos += len(line) + 1
                continue

            is_heading = (
                stripped.startswith('#') or
                re.match(r'^(\d+\.)+\d*\.?\s+[A-Z\u4e00-\u9fff]', stripped) is not None or
                re.match(r'^第[一二三四五六七八九十百]+[章节部分]\s', stripped) is not None
            )

            if is_heading and not stripped.startswith('#'):
                prev_line = lines[i - 1].strip() if i > 0 else ""
                if prev_line and not prev_line.startswith('#'):
                    dot_count = len(re.findall(r'\.', stripped.split()[0])) if stripped.split() else 0
                    if dot_count < 2:
                        if not (prev_line and prev_line[-1] in '.。!！?？:：;；'):
                            is_heading = False

            if is_heading:
                section_type = AcademicStructureDetector.detect_section_type(line)
                title = AcademicStructureDetector.extract_section_title(line) or stripped
                boundaries.append((title, current_pos, -1, section_type))

            current_pos += len(line) + 1

        # 填充结束位置
        for i in range(len(boundaries)):
            if i < len(boundaries) - 1:
                boundaries[i] = (boundaries[i][0], boundaries[i][1],
                                 boundaries[i + 1][1], boundaries[i][3])
            else:
                boundaries[i] = (boundaries[i][0], boundaries[i][1],
                                 len(text), boundaries[i][3])

        return boundaries

    @staticmethod
    def _is_ocr_noise(line: str) -> bool:
        """检测 OCR 噪声行"""
        if not line:
            return False
        if not line.startswith('#'):
            if len(line) < 5 or len(line) > 200:
                return True
        if re.match(r'^(Figure|Fig\.?|Table|Tab\.?|图|表)\s*[\d.:]+', line, re.IGNORECASE):
            return True
        alpha_chars = sum(1 for c in line if c.isalpha() or '\u4e00' <= c <= '\u9fff')
        if len(line) > 10 and alpha_chars / len(line) < 0.4:
            return True
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
        if '......' in line or '…' in line:
            return True
        camel_or_concat = re.findall(r'[a-z][A-Z][a-z]', line)
        if len(camel_or_concat) >= 3:
            return True
        if line.count('/') + line.count('|') >= 4:
            return True
        return False

    def _merge_to_sections(
        self,
        base_chunks: List[Tuple[str, int, int]],
        max_section_chars: int = 3000
    ) -> List[SmartChunk]:
        """将基础块合并为章节 — 内容感知版本"""
        if not base_chunks:
            return []

        sections = []
        current_group: List[Tuple[str, int, int]] = []
        current_size = 0

        for chunk in base_chunks:
            chunk_text, chunk_start, chunk_end = chunk
            chunk_len = len(chunk_text)
            should_break = False

            if current_group:
                if current_size + chunk_len > max_section_chars:
                    should_break = True

                first_line = chunk_text.split('\n')[0].strip()
                if (first_line.startswith('#') or
                    re.match(r'^(\d+\.)+\d*\.?\s+[A-Z\u4e00-\u9fff]', first_line) or
                    re.match(r'^第[一二三四五六七八九十百]+[章节部分]\s', first_line)):
                    should_break = True

                prev_text = current_group[-1][0]
                if prev_text.rstrip().endswith('\n') or chunk_text.lstrip().startswith('\n'):
                    if current_size >= max_section_chars * 0.5:
                        should_break = True

            if should_break and current_group:
                sections.append(self._build_section_chunk(current_group))
                current_group = []
                current_size = 0

            current_group.append(chunk)
            current_size += chunk_len

        if current_group:
            sections.append(self._build_section_chunk(current_group))

        return sections

    def _build_section_chunk(self, group: List[Tuple[str, int, int]]) -> SmartChunk:
        """从一组基础块构建章节级 SmartChunk"""
        content = '\n\n'.join(chunk[0] for chunk in group)
        start = group[0][1]
        end = group[-1][2]

        first_text = group[0][0]
        section_title = AcademicStructureDetector.extract_section_title(first_text)
        section_type = AcademicStructureDetector.detect_section_type(first_text)
        chunk_id = generate_chunk_id(content, start)

        metadata = ChunkMetadata(
            level=ChunkLevel.SECTION,
            section_type=section_type,
            section_title=section_title,
            has_citations=AcademicStructureDetector.has_citations(content)
        )

        return SmartChunk(
            id=chunk_id, content=content,
            start_char=start, end_char=end, metadata=metadata
        )

    def _create_document_chunk(self, text: str) -> SmartChunk:
        """创建文档级分块（摘要）"""
        summary = self._extract_document_summary(text)
        chunk_id = generate_chunk_id(text, 0)

        metadata = ChunkMetadata(
            level=ChunkLevel.DOCUMENT,
            has_citations=AcademicStructureDetector.has_citations(text)
        )

        return SmartChunk(
            id=chunk_id, content=summary,
            start_char=0, end_char=len(text), metadata=metadata
        )

    def _extract_document_summary(self, text: str, max_length: int = 1500) -> str:
        """提取文档摘要"""
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
                if (child.start_char >= parent.start_char and
                    child.end_char <= parent.end_char):
                    parent.metadata.child_ids.append(child.id)
                    child.metadata.parent_id = parent.id

    @staticmethod
    def _generate_chunk_id(content: str, position: int) -> str:
        """向后兼容：委托到模块级函数"""
        return generate_chunk_id(content, position)
