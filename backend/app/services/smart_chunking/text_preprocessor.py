"""
智能分块 - 文本预处理

职责：
- 文本清洗（统一换行、去多余空白）
- PDF OCR 噪声块移除
- 分句（中英文混合 + 引用保护）
"""
import re
from typing import List, Optional

from loguru import logger

from .types import ChunkConfig


# ============== 文本预处理 ==============

_CAPTION_PATTERN = re.compile(
    r'^(Figure|Fig\.?|Table|Tab\.?|图|表)\s*\d',
    re.IGNORECASE,
)


def preprocess_text(
    text: str,
    file_type: str = "txt",
    enable_ocr_noise_cleanup: bool = True,
) -> str:
    """
    预处理文本：统一换行、去多余空白、移除 OCR 噪声。

    适用于所有分块策略的前置步骤。
    """
    # 统一换行符
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    # 移除多余空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    # 移除多余空格
    text = re.sub(r' {2,}', ' ', text)
    normalized_file_type = (file_type or "txt").lower().replace(".", "")

    # OCR 噪声清理只在 PDF 场景下触发，且需检测到明显噪声信号
    if (
        enable_ocr_noise_cleanup
        and normalized_file_type == "pdf"
        and _should_apply_heuristic_ocr_cleanup(text)
    ):
        text = _strip_figure_noise_blocks(text)

    return text.strip()


# ============== 分句 ==============

def split_to_sentences(
    text: str,
    config: ChunkConfig,
    *,
    max_semantic_chars: Optional[int] = None,
) -> List[str]:
    """
    将文本分割为句子 — 带引用保护。

    当 config.preserve_citations=True 时，以引用标记开头的句子
    （如 "[1] ..." 或 "(Author, 2020) ..."）会与前一个句子合并，
    避免引用在分块时被截断导致语义割裂。

    参数:
        text: 待分句的文本
        config: 分块配置（用于引用保护开关和 max_semantic_chunk 限制）

    返回:
        句子列表
    """
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

    # 引用保护：将以引用标记开头的句子合并到前一句
    if config.preserve_citations and len(result) > 1:
        merged = [result[0]]
        citation_start_pattern = re.compile(
            r'^\s*(\[[\d,\s\-]+\]|'                             # [1], [1,2], [1-3]
            r'\([A-Z][a-z]+(?:\s+(?:et\s+al\.?|and|&))?.+\d{4}\)|'  # (Author, 2020)
            r'[A-Z][a-z]+(?:\s+et\s+al\.?)?\s*\(\d{4}\))'      # Author (2020)
        )
        citation_merge_limit = int(max_semantic_chars or config.max_semantic_chunk)
        for sentence in result[1:]:
            if citation_start_pattern.match(sentence) and merged:
                # 以引用开头 → 合并到前一句
                prev = merged[-1]
                combined = prev + ' ' + sentence
                if len(combined) <= citation_merge_limit:
                    merged[-1] = combined
                else:
                    merged.append(sentence)
            else:
                merged.append(sentence)
        result = merged

    return result


# ============== OCR 噪声清理 ==============

def _is_caption_line(line: str) -> bool:
    """判断是否为 Figure/Table caption 行。"""
    return bool(_CAPTION_PATTERN.match(line.strip()))


def _should_apply_heuristic_ocr_cleanup(text: str) -> bool:
    """
    是否启用启发式 OCR 清洗。
    仅当文本中存在明显“图表碎片 + caption”信号时触发，避免误删正文。
    """
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    if len(lines) < 12:
        return False

    fragment_count = sum(1 for line in lines if _is_fragment_line(line))
    fragment_ratio = fragment_count / len(lines)
    caption_count = sum(1 for line in lines if _is_caption_line(line))

    # 规则1：出现 caption 且碎片比例达到阈值
    if caption_count >= 1 and fragment_count >= 3 and fragment_ratio >= 0.08:
        return True

    # 规则2：大量短碎片行（无 caption 的兜底）
    short_fragment_count = sum(
        1 for line in lines
        if len(line) <= 20 and _is_fragment_line(line)
    )
    if fragment_count >= 8 and short_fragment_count >= 6 and fragment_ratio >= 0.20:
        return True

    return False

def _strip_figure_noise_blocks(text: str) -> str:
    """
    检测并移除 pypdf 提取的图表/表格内部噪声文本块。

    pypdf 从 PDF 中提取文本时，会把 Figure/Table 内部的标注文字
    （如架构图里的 "SAM", "VITDET", "80M" 等）提取为一连串短碎片行。

    检测规则：
    - 连续 3+ 行碎片（短、非句子、非标题）
    - 紧跟在 Figure/Table caption 前，或平均行长 < 40 且无完整句子
    """
    lines = text.split('\n')
    cleaned_lines = []
    i = 0

    while i < len(lines):
        if _is_fragment_line(lines[i]):
            block_start = i
            while i < len(lines) and _is_fragment_line(lines[i]):
                i += 1

            block_length = i - block_start

            if block_length >= 3:
                next_line = lines[i].strip() if i < len(lines) else ""
                is_before_caption = _is_caption_line(next_line)

                block_lines = lines[block_start:i]
                avg_len = sum(len(l.strip()) for l in block_lines) / max(block_length, 1)
                has_sentence = any(
                    l.strip().endswith(('.', '。', '!', '！', '?', '？'))
                    and len(l.strip()) > 30
                    for l in block_lines
                )

                if is_before_caption or (avg_len < 40 and not has_sentence):
                    logger.debug(
                        f"移除图表噪声块: {block_length} 行, "
                        f"avg_len={avg_len:.0f}, 内容='{block_lines[0].strip()[:50]}...'"
                    )
                    continue
                else:
                    cleaned_lines.extend(block_lines)
                    continue
            else:
                cleaned_lines.extend(lines[block_start:i])
                continue

        cleaned_lines.append(lines[i])
        i += 1

    return '\n'.join(cleaned_lines)


def _is_fragment_line(line: str) -> bool:
    """
    判断一行是否为图表碎片行（pypdf 从图中提取的短文字标签）。

    碎片行特征：短、非句子、非标题、含大量缩写/数字/特殊符号。
    """
    stripped = line.strip()
    if not stripped:
        return False

    if len(stripped) > 60:
        return False

    # 正常章节标题不是碎片
    if stripped.startswith('#'):
        return False
    if re.match(r'^(\d+\.)+\d*\.?\s+[A-Z\u4e00-\u9fff]', stripped):
        return False
    if re.match(r'^第[一二三四五六七八九十百]+[章节部分]', stripped):
        return False

    # Figure/Table caption 不是碎片
    if _is_caption_line(stripped):
        return False

    if len(stripped) > 30 and stripped[-1] in '.。!！?？':
        return False

    if len(stripped) < 20:
        return True

    if stripped[-1] not in '.。,，;；:：!！?？)）]】"\'':
        alpha_chars = sum(1 for c in stripped if c.isalpha() or '\u4e00' <= c <= '\u9fff')
        if alpha_chars / max(len(stripped), 1) < 0.6:
            return True

    return False
